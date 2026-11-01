"""Bounded resource work; only the coordinator updates course observations."""
import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from base.api_client import RequestRateLimited
from base.auth import AuthenticationExpired
from reporting import Reporter


def check_study_response(response, *, parsed=False):
    """Accept legacy message acknowledgements, but never an explicit failure code."""
    if not isinstance(response, dict):
        raise RuntimeError('平台未确认学习记录保存成功')
    code = response.get('code')
    if str(code) == '429':
        raise RequestRateLimited('平台请求过于频繁，请稍后重试')
    if str(code) in ('401', '403'):
        raise AuthenticationExpired('平台登录已过期或无权访问')
    if code is not None and str(code) != '200':
        raise RuntimeError('平台未确认学习记录保存成功')
    if code is None and not (response.get('msg') or (parsed and response)):
        raise RuntimeError('平台未确认学习记录保存成功')


def unique_resources(resources):
    seen = set()
    for args in resources:
        node = args[0]
        key = node.get('id')
        if key is None or key in seen:
            continue
        seen.add(key)
        yield args


def _close_worker_client(handler, client):
    try:
        client.close()
    except Exception as error:
        # Cleanup must not replace the original request failure.
        handler.logging.error('关闭学习连接失败: %s', error)


def _resource_worker(handler, args, client):
    worker = copy.copy(handler)
    worker.progress = Reporter()
    worker._course_failed = False
    worker._course_processed = False
    if hasattr(handler, 'study_record_list'):
        worker.study_record_list = tuple(handler.study_record_list)
    worker.client = client
    try:
        if handler.cancel_event.is_set():
            return 'cancelled'
        return worker.handle_resource(*args)
    except BaseException:
        handler.cancel_event.set()
        raise
    finally:
        _close_worker_client(handler, worker.client)


def run_resources(handler, resources, course_name='课程'):
    # Discover the complete deduplicated workload before dispatching any writes.
    discovered = []
    for args in unique_resources(resources):
        if handler.cancel_event.is_set():
            handler._course_failed = True
            return
        discovered.append(args)
    if handler.cancel_event.is_set():
        handler._course_failed = True
        return
    total = len(discovered)
    resources = iter(discovered)
    completed = 0
    counts = {'success': 0, 'skipped': 0, 'error': 0}
    handler.progress.resources(f'{course_name} · 正在处理资源', 0, total)

    def observe(node, status):
        nonlocal completed
        if status == 'cancelled':
            handler._course_failed = True
            return
        completed += 1
        counts[status] += 1
        handler._course_failed |= status == 'error'
        handler._course_processed |= status == 'success'
        name = node.get('name', '资源')
        done = status == 'success' or node.get('id') in getattr(handler, 'study_record_list', ())
        handler.progress.unit(node.get('id'), name, 'done' if done else 'failed' if status == 'error' else status)
        summary = f"成功 {counts['success']} · 跳过 {counts['skipped']} · 失败 {counts['error']}"
        handler.progress.resources(f'{course_name} · {name} · {summary}', completed, total)

    if handler.max_workers == 1:
        for args in resources:
            if handler.cancel_event.is_set():
                handler._course_failed = True
                break
            handler.progress.unit(args[0].get('id'), args[0].get('name', '资源'), 'running')
            try:
                status = handler.handle_resource(*args)
            except BaseException as error:
                handler.cancel_event.set()
                observe(args[0], 'error' if isinstance(error, Exception) else 'cancelled')
                raise
            observe(args[0], status)
        return

    pool = ThreadPoolExecutor(max_workers=handler.max_workers, thread_name_prefix='study')
    pending = {}

    def collect(future, node):
        try:
            status = future.result()
        except BaseException as error:
            observe(node, 'error' if isinstance(error, Exception) else 'cancelled')
            if isinstance(error, (AuthenticationExpired, RequestRateLimited)) or not isinstance(error, Exception):
                handler.cancel_event.set()
                return error
            handler.logging.error('处理资源失败: %s', error)
        else:
            observe(node, status)
        return None

    original_error = None
    try:
        exhausted = False
        while pending or not exhausted:
            while not exhausted and not handler.cancel_event.is_set() and len(pending) < handler.max_workers:
                try:
                    args = next(resources)
                except StopIteration:
                    exhausted = True
                    break
                # Snapshot authentication only on the coordinator.
                client = handler.client.clone()
                try:
                    future = pool.submit(_resource_worker, handler, args, client)
                except BaseException:
                    _close_worker_client(handler, client)
                    raise
                pending[future] = (args[0], client)
                handler.progress.unit(args[0].get('id'), args[0].get('name', '资源'), 'running')
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            fatal = None
            for future in done:
                node, _ = pending.pop(future)
                error = collect(future, node)
                if fatal is None:
                    fatal = error
            if fatal is not None:
                raise fatal
            if handler.cancel_event.is_set():
                handler._course_failed = True
                break
    except BaseException as error:
        original_error = error
        handler.cancel_event.set()
        raise
    finally:
        for future, (_, client) in pending.items():
            if future.cancel():
                _close_worker_client(handler, client)
        pool.shutdown(wait=True)
        # Requests already in flight may finish after cancellation. Report their
        # actual outcomes on this coordinator without masking the original error.
        drain_error = None
        for future, (node, _) in pending.items():
            if not future.cancelled():
                error = collect(future, node)
                if drain_error is None:
                    drain_error = error
        if original_error is None and drain_error is not None:
            raise drain_error
