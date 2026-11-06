import copy

from MoocMain.log import Logger
from base.auth import AuthenticationExpired
from base.quiet_hours import checkpoint
from base.util import create_session, parse_response

logger = Logger(__name__).get_log()


class RequestRateLimited(RuntimeError):
    """The platform rejected the request budget; stop dispatching work."""


class BaseAPIClient:
    """Common platform HTTP operations, with bounded waits and no write retries."""

    def __init__(self, base_url):
        self.session = create_session()
        self.default_base_url = base_url
        self.access_token = None

    def set_auth_token(self, token):
        self.access_token = token
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def clone(self):
        """Copy authenticated state without invoking platform constructors/login."""
        client = copy.copy(self)
        client.session = create_session()
        try:
            for name in ('headers', 'cookies', 'params', 'proxies'):
                setattr(client.session, name, copy.deepcopy(getattr(self.session, name)))
            for name in ('auth', 'verify', 'cert', 'trust_env', 'max_redirects'):
                setattr(client.session, name, copy.deepcopy(getattr(self.session, name)))
            return client
        except BaseException:
            client.session.close()
            raise

    def close(self):
        self.session.close()

    def _request(self, method, endpoint, base_url=None, origin_response=False, **kwargs):
        checkpoint()
        response = None
        try:
            response = getattr(self.session, method)(
                f"{base_url or self.default_base_url}{endpoint}",
                timeout=(10, 30), allow_redirects=False, **kwargs)
            if response.status_code == 429:
                raise RequestRateLimited('平台请求过于频繁，请稍后重试')
            if response.status_code in (401, 403):
                raise AuthenticationExpired('平台登录已过期或无权访问')
            if response.status_code != 200:
                logger.error('平台返回 HTTP %s', response.status_code)
                return None
            data = response.json()
            if isinstance(data, dict) and str(data.get('code')) == '429':
                raise RequestRateLimited('平台请求过于频繁，请稍后重试')
            if isinstance(data, dict) and str(data.get('code')) in ('401', '403'):
                raise AuthenticationExpired('平台登录已过期或无权访问')
            return data if origin_response else parse_response(response)
        except (AuthenticationExpired, RequestRateLimited):
            raise
        except Exception as error:
            logger.error('%s 请求失败: %s', method.upper(), error)
            return None
        finally:
            if response is not None:
                response.close()

    def get(self, endpoint, params=None, base_url=None, origin_response=False):
        return self._request('get', endpoint, base_url, origin_response, params=params)

    def post(self, endpoint, json=None, data=None, base_url=None, origin_response=False):
        return self._request('post', endpoint, base_url, origin_response, json=json, data=data)

    def put(self, endpoint, json=None, data=None, base_url=None):
        return self._request('put', endpoint, base_url, json=json, data=data)

    def login(self):
        raise NotImplementedError('子类必须实现 login() 方法')
