from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    
    if response is not None:
        custom_response_data = {
            'success': False,
            'message': _extract_error_message(response.data),
            'code': response.status_code,
        }
        
        if response.status_code >= 500:
            logger.error(
                f"Server Error {response.status_code}: "
                f"{context['request'].method} {context['request'].path} - {str(exc)}"
            )
        elif response.status_code >= 400:
            logger.warning(
                f"Client Error {response.status_code}: "
                f"{context['request'].method} {context['request'].path}"
            )
        
        return Response(custom_response_data, status=response.status_code)
    
    return response


def _extract_error_message(data):
    def first_message(value):
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return first_message(value[0])
        if isinstance(value, dict):
            if 'detail' in value:
                return first_message(value['detail'])
            if 'message' in value:
                return first_message(value['message'])
            first_key = next(iter(value), None)
            if first_key is not None:
                return first_message(value[first_key])
            return ''
        return str(value)

    if isinstance(data, dict):
        if 'detail' in data:
            return first_message(data['detail'])
        if 'message' in data:
            return first_message(data['message'])
        first_key = next(iter(data), None)
        if first_key:
            return first_message(data[first_key])
    elif isinstance(data, list) and len(data) > 0:
        return first_message(data[0])
    return first_message(data) or '请求处理失败'
