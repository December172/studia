#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OAuth登录处理器
用于处理基于重定向的登录流程
"""

import socket
import threading
import webbrowser
import time
import random
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from MoocMain.log import Logger
from NewMoocMain.oauth_pages import render_oauth_page

logger = Logger(__name__).get_log()

SSO_AUTH_URL = "https://sso.icve.com.cn/sso/auth"
# 官方退出路由：清除 .icve.com.cn 下的 token cookie，再进入带本地回调的登录页。
SSO_LOGOUT_URL = "https://sso.icve.com.cn/loginOut"
SSO_SOURCE = "2"

class OAuthLoginHandler:
    """OAuth登录处理器"""
    
    def __init__(self, port=None):
        self.port = port
        self.token = None
        self.server = None
        self.server_thread = None
        self.is_running = False
        self._stop_lock = threading.Lock()
        
    def find_available_port(self, preferred_port=None, max_attempts=50):
        """
        找到一个可用的端口
        
        Args:
            preferred_port: 首选端口，如果为None则随机选择
            max_attempts: 最大尝试次数
            
        Returns:
            可用的端口号
        """
        attempted_ports = set()
        
        # 如果有首选端口，先尝试
        if preferred_port and preferred_port not in attempted_ports:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', preferred_port))
                    logger.debug(f" 首选端口 {preferred_port} 可用")
                    return preferred_port
            except OSError:
                logger.debug(f" 首选端口 {preferred_port} 被占用")
                attempted_ports.add(preferred_port)
        
        # 随机尝试端口
        for attempt in range(max_attempts):
            # 使用随机端口范围：30000-65535（避免系统保留端口）
            port = random.randint(30000, 65535)
            
            # 跳过已尝试过的端口
            if port in attempted_ports:
                continue
            
            attempted_ports.add(port)
            
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', port))
                    logger.debug(f" 随机端口 {port} 可用 (尝试第{attempt + 1}次)")
                    return port
            except OSError:
                logger.debug(f" 端口 {port} 被占用")
                continue
        
        # 如果随机尝试失败，尝试常用端口范围
        logger.debug(" 随机端口尝试失败，尝试常用端口范围...")
        common_ports = list(range(39001, 39050)) + list(range(38000, 38050)) + list(range(37000, 37050))
        random.shuffle(common_ports)  # 随机打乱顺序
        
        for port in common_ports:
            if port in attempted_ports:
                continue
                
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', port))
                    logger.debug(f" 常用端口 {port} 可用")
                    return port
            except OSError:
                continue
        
        raise Exception(f"❌ 无法找到可用端口 (已尝试 {len(attempted_ports)} 个端口)")
    
    def create_request_handler(self):
        """Handle the callback; presentation lives in oauth_pages.py."""
        oauth_handler = self

        class RequestHandler(BaseHTTPRequestHandler):
            def setup(self):
                # Browsers may preconnect without sending a request. Bound each
                # connection's lifetime, independently of the callback listener.
                self.request.settimeout(5)
                super().setup()

            def do_GET(self):
                parsed_url = urlparse(self.path)
                token = None
                if parsed_url.path == '/':
                    status, state = 200, 'waiting'
                elif parsed_url.path == '/login':
                    tokens = parse_qs(parsed_url.query).get('token', [])
                    token = tokens[0] if tokens and tokens[0].strip() else None
                    status, state = (200, 'success') if token else (400, 'error')
                else:
                    status, state = 404, 'not_found'

                body = render_oauth_page(state).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy',
                                 "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                 "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if token:
                    # Deliver the whole response before waking the waiting application.
                    oauth_handler.token = token
                    logger.debug("登录回调成功（凭证已隐藏）")

            def log_message(self, format, *args):
                # Callback URLs contain credentials. Do not log request paths.
                pass

        return RequestHandler

    def start_server(self, retry_count=3):
        """
        启动HTTP服务器
        
        Args:
            retry_count: 重试次数
            
        Returns:
            成功返回True，失败返回False
        """
        for attempt in range(retry_count):
            try:
                # 找到可用端口
                self.port = self.find_available_port(self.port)
                
                # 创建HTTP服务器
                self.server = ThreadingHTTPServer(('127.0.0.1', self.port), self.create_request_handler())
                self.is_running = True
                
                logger.info(f" OAuth服务已就绪，端口: {self.port}")
                logger.debug(f"   回调地址: http://localhost:{self.port}/login")

                # 在新线程中运行服务器
                self.server_thread = threading.Thread(target=self.server.serve_forever,
                                                      kwargs={"poll_interval": 0.05})
                self.server_thread.daemon = True
                self.server_thread.start()
                
                return True
                
            except Exception as e:
                logger.warning(f" 启动OAuth登录服务器失败 (尝试第{attempt + 1}次): {e}")
                
                # 如果不是最后一次尝试，重置端口并重试
                if attempt < retry_count - 1:
                    self.port = None  # 重置端口，下次尝试时会重新随机选择
                    time.sleep(1)  # 稍等片刻再重试
                    continue
                else:
                    logger.error(f" 启动OAuth登录服务器失败 (已重试{retry_count}次)")
                    return False
        
        return False
    
    def stop_server(self):
        """停止HTTP服务器"""
        with self._stop_lock:
            if not self.server or not self.is_running:
                return
            server = self.server
            self.is_running = False
        # ThreadingHTTPServer has daemon request workers; idle browser sockets
        # cannot block shutdown or keep the application alive on Ctrl+C.
        server.shutdown()
        server.server_close()
        logger.info(" OAuth登录服务器已停止")
    
    def open_login_page(self, force_logout=False):
        """
        打开登录页面

        Args:
            force_logout: 先经官方 /loginOut 清除浏览器共享的 SSO cookie，再回到登录页。
                          仅用于用户显式退出后；普通登录不传。
        """
        # 官方路由把 redirect 原样拼进 /sso/auth?source=...&redirect=...，
        # 所以这里只给本地回调；嵌套登录页会被它的正则截断。
        redirect_url = f"http://localhost:{self.port}/login"
        login_url = f"{SSO_AUTH_URL}?" + urlencode(
            {"mode": "simple", "source": SSO_SOURCE, "redirect": redirect_url})
        if force_logout:
            login_url = f"{SSO_LOGOUT_URL}?" + urlencode(
                {"source": SSO_SOURCE, "redirect": redirect_url})

        logger.info(f" 浏览器已打开，请完成登录...")

        try:
            webbrowser.open(login_url)
            return True
        except Exception as e:
            logger.error(f" 无法打开浏览器: {e}")
            logger.info(f"请手动打开以下URL进行登录: {login_url}")
            return False
    
    def wait_for_token(self, timeout=300):
        """等待token获取"""
        start_time = time.time()
        while self.token is None and time.time() - start_time < timeout:
            if not self.is_running:
                break
            time.sleep(0.5)
        
        if self.token:
            return self.token
        else:
            logger.error(" 登录超时")
            return None
    
    def login(self, timeout=300, force_logout=False):
        """
        执行OAuth登录流程

        Args:
            timeout: 超时时间（秒）
            force_logout: 先清除浏览器共享的 SSO 登录，再打开登录页

        Returns:
            成功返回token字符串，失败返回None
        """
        try:
            # 1. 启动HTTP服务器
            if not self.start_server():
                return None

            # 2. 打开登录页面
            self.open_login_page(force_logout=force_logout)
            
            # 3. 等待token获取
            token = self.wait_for_token(timeout)
            
            return token
            
        except Exception as e:
            logger.error(f" OAuth登录过程中发生异常: {e}")
            return None
        finally:
            # KeyboardInterrupt/SystemExit must take the same cleanup path.
            self.stop_server()
    
    def __del__(self):
        """析构函数，确保服务器被正确关闭"""
        self.stop_server()


def oauth_login(timeout=300, preferred_port=None, force_logout=False):
    """
    OAuth登录的便捷函数

    Args:
        timeout: 超时时间（秒）
        preferred_port: 首选端口，如果为None则随机选择
        force_logout: 先清除浏览器共享的 SSO 登录，再打开登录页

    Returns:
        成功返回token字符串，失败返回None
    """
    handler = OAuthLoginHandler(port=preferred_port)
    return handler.login(timeout, force_logout=force_logout)


if __name__ == "__main__":
    from ui import oauth_diagnostic
    oauth_diagnostic(oauth_login)
