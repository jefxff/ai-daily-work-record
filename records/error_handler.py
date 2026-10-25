#!/usr/bin/env python
"""
统一错误处理模块
提供重试机制、错误日志记录和统一错误响应
"""

import logging
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import wraps
from django.http import JsonResponse

# 配置日志
logger = logging.getLogger(__name__)


def create_error_response(message, code=None, details=None, status_code=400):
    """
    创建统一的错误响应
    
    Args:
        message: 错误消息
        code: 错误代码（可选）
        details: 详细错误信息（可选）
        status_code: HTTP状态码
    
    Returns:
        JsonResponse: 错误响应
    """
    error_data = {
        'success': False,
        'error': {
            'message': message,
            'code': code,
            'details': details
        },
        'timestamp': time.time()
    }
    
    # 清理空值
    if code is None:
        error_data['error'].pop('code')
    if details is None:
        error_data['error'].pop('details')
    
    return JsonResponse(error_data, status=status_code)


def create_success_response(data=None, message="操作成功", status_code=200):
    """
    创建统一的成功响应
    
    Args:
        data: 响应数据
        message: 成功消息
        status_code: HTTP状态码
    
    Returns:
        JsonResponse: 成功响应
    """
    response_data = {
        'success': True,
        'message': message,
        'timestamp': time.time()
    }
    
    if data is not None:
        response_data['data'] = data
    
    return JsonResponse(response_data, status=status_code)

class ErrorHandler:
    """错误处理器"""
    
    @staticmethod
    def create_retry_session(retries=3, backoff_factor=1, status_forcelist=None):
        """
        创建带有重试机制的会话
        
        Args:
            retries: 重试次数
            backoff_factor: 重试间隔因子
            status_forcelist: 需要重试的HTTP状态码列表
        
        Returns:
            requests.Session: 配置了重试机制的会话对象
        """
        if status_forcelist is None:
            status_forcelist = [429, 500, 502, 503, 504]
        
        session = requests.Session()
        retry_strategy = Retry(
            total=retries,
            backoff_factor=backoff_factor,
            status_forcelist=status_forcelist,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    @staticmethod
    def retry_on_exception(max_retries=3, delay=1, exceptions=(Exception,)):
        """
        异常重试装饰器
        
        Args:
            max_retries: 最大重试次数
            delay: 重试间隔（秒）
            exceptions: 需要重试的异常类型
        
        Returns:
            function: 装饰器函数
        """
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                last_exception = None
                for attempt in range(max_retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e
                        if attempt < max_retries:
                            logger.warning(
                                f"函数 {func.__name__} 第 {attempt + 1} 次尝试失败: {str(e)}"
                                f"，{delay}秒后重试..."
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"函数 {func.__name__} 所有 {max_retries} 次尝试均失败"
                            )
                            raise last_exception
                return None
            return wrapper
        return decorator
    
    @staticmethod
    def log_exception(func):
        """
        异常日志记录装饰器
        
        Args:
            func: 被装饰的函数
        
        Returns:
            function: 装饰器函数
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    f"函数 {func.__name__} 执行失败: {str(e)}",
                    exc_info=True
                )
                raise
        return wrapper

class APIErrorHandlerMiddleware:
    """API错误处理中间件"""
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        try:
            response = self.get_response(request)
            return response
        except APIError as e:
            logger.error(f"API错误: {e.message}", exc_info=True)
            return JsonResponse(e.to_dict(), status=e.status_code or 500)
        except Exception as e:
            logger.error(f"未处理的异常: {str(e)}", exc_info=True)
            error_response = create_error_response(
                message="服务器内部错误",
                status_code=500,
                details={"exception": str(e)}
            )
            return JsonResponse(error_response, status=500)

    def process_exception(self, request, exception):
        """处理视图函数中的异常"""
        if isinstance(exception, APIError):
            logger.error(f"API错误: {exception.message}", exc_info=True)
            return JsonResponse(exception.to_dict(), status=exception.status_code or 500)
        
        logger.error(f"未处理的异常: {str(exception)}", exc_info=True)
        error_response = create_error_response(
            message="服务器内部错误",
            status_code=500,
            details={"exception": str(exception)}
        )
        return JsonResponse(error_response, status=500)

    @staticmethod
    def log_exception(func):
        """
        异常日志记录装饰器
        
        Args:
            func: 被装饰的函数
        
        Returns:
            function: 装饰器函数
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    f"函数 {func.__name__} 执行失败: {str(e)}",
                    exc_info=True
                )
                raise
        return wrapper

class APIError(Exception):
    """API错误异常类"""
    
    def __init__(self, message, status_code=None, details=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}
    
    def to_dict(self):
        """转换为字典格式"""
        return {
            'error': True,
            'message': self.message,
            'status_code': self.status_code,
            'details': self.details
        }

# 删除重复的函数定义，保留前面的JsonResponse版本

# 示例/自测入口：原为 __main__ 下的 print 演示，按项目口径改为 logging
# （审查 W1：production 代码里不保留 print）。需手工验证时用下面的命令：
#   python -m records.error_handler
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    @ErrorHandler.retry_on_exception(max_retries=3, delay=1)
    @ErrorHandler.log_exception
    def test_function():
        """测试函数"""
        logger.info("执行测试函数...")
        raise ValueError("测试异常")

    try:
        test_function()
    except Exception as exc:
        logger.info("捕获异常: %s", exc)

    logger.info("错误响应: %s", create_error_response("测试错误", 400, {"field": "value"}))
    logger.info("成功响应: %s", create_success_response({"result": "success"}, "操作成功"))