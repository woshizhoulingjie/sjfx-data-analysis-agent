"""Administrator-only routes, independent of the legacy global API token."""
from urllib.parse import urlsplit
import threading

from services.model_diagnostics import test_models
from services.model_settings import get_model_settings, save_model_settings


def register_model_settings_routes(app, *, config, session_resolver, request_api=None, json_response=None):
    if request_api is None or json_response is None:
        from web_compat import request, jsonify
        request_api = request_api or request
        json_response = json_response or jsonify
    diagnostic_gate = threading.BoundedSemaphore(1)

    def guard(write=False):
        session = session_resolver()
        if not session:
            return json_response({'ok': False, 'error': '未登录'}), 401
        if session.get('role') != 'admin':
            return json_response({'ok': False, 'error': '需要管理员权限'}), 403
        if session.get('must_change_pw'):
            return json_response({'ok': False, 'error': '首次登录必须先修改初始密码'}), 403
        if write:
            if request_api.headers.get('X-SJFX-Model-Settings') != '1':
                return json_response({'ok': False, 'error': '缺少模型设置请求标识'}), 403
            origin = request_api.headers.get('Origin')
            if origin:
                try:
                    parsed = urlsplit(origin)
                    if (parsed.scheme not in {'http', 'https'} or parsed.netloc.lower() != request_api.headers.get('Host', '').lower()
                            or parsed.username is not None or parsed.password is not None
                            or parsed.path or parsed.query or parsed.fragment):
                        raise ValueError
                except ValueError:
                    return json_response({'ok': False, 'error': '模型设置请求来源无效'}), 403
        return None

    @app.route('/api/admin/model-settings', methods=['GET'])
    def model_settings_get():
        denied = guard()
        if denied is not None:
            return denied
        try:
            return json_response(get_model_settings(config))
        except (ValueError, RuntimeError):
            return json_response({'ok': False, 'error': '无法读取有效模型配置'}), 400

    @app.route('/api/admin/model-settings', methods=['PUT'])
    def model_settings_put():
        denied = guard(write=True)
        if denied is not None:
            return denied
        try:
            return json_response(save_model_settings(config, request_api.get_json(silent=True)))
        except (ValueError, RuntimeError) as exc:
            return json_response({'ok': False, 'error': str(exc)}), 400
        except OSError:
            return json_response({'ok': False, 'error': '无法保存模型配置，请检查实例目录权限'}), 500

    @app.route('/api/admin/model-test', methods=['POST'])
    def model_settings_test():
        denied = guard(write=True)
        if denied is not None:
            return denied
        if not diagnostic_gate.acquire(blocking=False):
            return json_response({'ok': False, 'error': '已有模型诊断正在运行，请稍后重试'}), 429
        try:
            body = request_api.get_json(silent=True)
            if not isinstance(body, dict) or set(body) - {'role', 'settings', 'timeout_seconds'}:
                raise ValueError('无效诊断请求')
            return json_response(test_models(config, payload=body.get('settings'),
                role=body.get('role', 'all'), timeout_seconds=body.get('timeout_seconds', 20)))
        except (ValueError, RuntimeError) as exc:
            return json_response({'ok': False, 'error': str(exc)}), 400
        finally:
            diagnostic_gate.release()
