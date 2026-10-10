"""Bounded AI generation delivered as completed; the caller owns paper state."""
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
import threading
import time

from ai.client import AIClient, AIError, AIServiceError
from ai.questions import model_question, prepare_question


def prepared_items(raw_questions):
    """Prepare identities before dispatch, retaining invalid rows in paper order."""
    items, identities = [], set()
    for number, raw in enumerate(raw_questions):
        children = raw.get('questionSubList') if isinstance(raw, dict) else None
        if children is not None and not isinstance(children, list):
            raise ValueError('子题结构未知')
        work = [(child, raw, i) for i, child in enumerate(children)] if children else [(raw, None, None)]
        for value, parent, child_number in work:
            try:
                q = prepare_question(value, number, parent, child_number)
                identity = (q['paper_id'], q['paper_sub_id'])
                if identity in identities:
                    raise ValueError('试卷包含重复试题 ID')
                identities.add(identity)
                items.append((value, q, None))
            except (ValueError, TypeError) as error:
                items.append((value, None, error))
    return items


def prepared_questions(raw_questions):
    return [q for _, q, _ in prepared_items(raw_questions)]


class ParallelAnswers:
    """Bound pending requests and buffered answers together.

    Refill only when the caller resumes iteration after handling an answer.
    Manual input drains in-flight requests without dispatching replacements.
    """
    def __init__(self, ai, questions, max_workers, clock, allow_manual=False):
        self.config = replace(ai.config)
        self.questions = questions
        self.max_workers = max_workers
        self.clock = clock
        self.allow_manual = allow_manual
        self.fatal_error = None
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='ai-answer')
        self.stopped = threading.Event()
        self.cooldown_lock = threading.Lock()
        self.cooldown_until = 0.0
        self.results = {}
        self.futures = {}
        self.next_index = 0

    def _wait_for_service(self):
        while not self.stopped.is_set():
            with self.cooldown_lock:
                delay = self.cooldown_until - time.monotonic()
            if delay <= 0:
                return
            self.stopped.wait(delay)
        raise CancelledError()

    def _generate(self, q):
        client = AIClient(replace(self.config))
        try:
            for attempt in range(3):
                self._wait_for_service()
                if self.stopped.is_set():
                    raise CancelledError()
                try:
                    return client.answer(model_question(q))
                except AIError as exc:
                    if not exc.retryable:
                        raise
                    delay = max(attempt + 1, getattr(exc, 'retry_after', 0))
                    if isinstance(exc, AIServiceError):
                        with self.cooldown_lock:
                            self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
                    if attempt == 2:
                        failure = type(exc)('单题重试 2 次后仍失败：' + str(exc))
                        if hasattr(exc, 'retry_after'):
                            failure.retry_after = exc.retry_after
                        raise failure from None
                    if not isinstance(exc, AIServiceError) and self.stopped.wait(delay):
                        raise CancelledError()
        finally:
            client.session.close()

    def _collect(self, done):
        for future in done:
            position = self.futures.pop(future)
            try:
                self.results[position] = future.result()
            except AIError as exc:
                self.results[position] = exc
                if isinstance(exc, AIServiceError) and not (exc.retryable and self.allow_manual):
                    self.fatal_error = exc
                    self.stopped.set()
                    for pending in self.futures:
                        pending.cancel()
            except CancelledError:
                self.results[position] = AIServiceError('AI 服务失败，本题已停止：' + str(self.fatal_error))

    def drain(self):
        """Finish existing requests before manual input, without filling slots."""
        while self.futures:
            self.clock.check()
            done, _ = wait(self.futures, timeout=0.05, return_when=FIRST_COMPLETED)
            self._collect(done)

    def answers(self):
        """Yield (original position, answer or error) as results become ready."""
        while self.next_index < len(self.questions) or self.futures or self.results:
            self.clock.check()
            self._collect([future for future in self.futures if future.done()])
            # Handle buffered results before starting anything else: a result
            # may need manual input or stop this paper entirely.
            if not self.results and not self.stopped.is_set():
                while (self.next_index < len(self.questions) and
                       len(self.futures) + len(self.results) < self.max_workers):
                    self.clock.check()
                    position = self.next_index
                    self.next_index += 1
                    q = self.questions[position]
                    if q is None:
                        self.results[position] = None
                        break
                    self.futures[self.executor.submit(self._generate, q)] = position
            if not self.results and self.futures:
                done, _ = wait(self.futures, timeout=0.05, return_when=FIRST_COMPLETED)
                self._collect(done)
            if self.results:
                # A service failure must stop dispatch even if another answer
                # happened to complete at the same time.
                position = min(self.results, key=lambda i: (
                    not isinstance(self.results[i], AIServiceError), i))
                yield position, self.results.pop(position)
            elif self.stopped.is_set():
                return

    def close(self):
        self.stopped.set()
        for future in self.futures:
            future.cancel()
        # Active HTTP calls retain their existing connect/read timeouts. No new
        # retry starts after stop, and all owned sessions close before returning.
        self.executor.shutdown(wait=True, cancel_futures=True)
