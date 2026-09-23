import os
import json
import logging
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('callback_server.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

PORT = int(os.getenv('PORT', 8080))
VERIFY_FILE_PATH = os.path.join(os.path.dirname(__file__), 'steamdt_verify.txt')


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/steamdt_verify.txt':
            self._handle_verify_file()
        elif self.path == '/health':
            self._handle_health_check()
        else:
            self._send_response(404, {'error': 'Not Found'})

    def do_POST(self):
        try:
            content_len = int(self.headers.get('Content-Length', 0))
            post_body = self.rfile.read(content_len)

            logger.info(f"\n{'='*50}")
            logger.info(f"收到回调请求 [{datetime.now().isoformat()}]")
            logger.info(f"路径: {self.path}")
            logger.info(f"客户端IP: {self.client_address[0]}")
            logger.info(f"Headers: {dict(self.headers)}")

            try:
                data = json.loads(post_body.decode('utf-8'))
                logger.info(f"Body (JSON): {json.dumps(data, indent=2, ensure_ascii=False)}")
            except ValueError:
                data = post_body.decode('utf-8')
                logger.info(f"Body (原始): {data}")

            response = self._process_callback(self.path, data)
            self._send_response(200, response)

            logger.info(f"响应: {json.dumps(response, ensure_ascii=False)}")
            logger.info(f"{'='*50}")

        except Exception as e:
            logger.error(f"处理回调请求异常: {str(e)}", exc_info=True)
            self._send_response(500, {'status': 'error', 'message': str(e)})

    def _handle_verify_file(self):
        try:
            if os.path.exists(VERIFY_FILE_PATH):
                with open(VERIFY_FILE_PATH, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(content.encode('utf-8'))
                logger.info(f"验证文件已返回: {VERIFY_FILE_PATH}")
            else:
                logger.warning(f"验证文件不存在: {VERIFY_FILE_PATH}")
                self._send_response(404, {'error': 'Verification file not found'})
        except Exception as e:
            logger.error(f"读取验证文件异常: {str(e)}")
            self._send_response(500, {'error': str(e)})

    def _handle_health_check(self):
        self._send_response(200, {'status': 'ok', 'service': 'steamdt_callback_server'})

    def _process_callback(self, path, data):
        handlers = {
            '/callback/authenticator': self._handle_authenticator_callback,
            '/callback/order': self._handle_order_callback,
            '/callback/trade': self._handle_trade_callback,
            '/callback/email': self._handle_email_callback,
            '/callback': self._handle_generic_callback,
        }

        handler = handlers.get(path, self._handle_generic_callback)
        return handler(data)

    def _handle_authenticator_callback(self, data):
        logger.info("处理认证器回调")
        callback_type = data.get('type', 'unknown')

        if callback_type == 'authenticator_added':
            return self._handle_authenticator_added(data)
        elif callback_type == 'authenticator_finalized':
            return self._handle_authenticator_finalized(data)
        elif callback_type == 'code_required':
            return self._handle_code_required(data)
        else:
            logger.warning(f"未知的认证器回调类型: {callback_type}")
            return {'status': 'ok', 'received': True}

    def _handle_authenticator_added(self, data):
        steamid = data.get('steamid')
        shared_secret = data.get('shared_secret')
        serial_number = data.get('serial_number')

        logger.info(f"认证器已添加 - SteamID: {steamid}, Serial: {serial_number}")

        if shared_secret:
            logger.info(f"Shared Secret 已收到，长度: {len(shared_secret)}")

        return {'status': 'ok', 'message': 'Authenticator added callback received'}

    def _handle_authenticator_finalized(self, data):
        steamid = data.get('steamid')
        success = data.get('success', False)

        logger.info(f"认证器最终化 - SteamID: {steamid}, Success: {success}")

        if success:
            logger.info(f"SteamID {steamid} 的认证器已成功激活")
        else:
            logger.error(f"SteamID {steamid} 的认证器激活失败")

        return {'status': 'ok', 'success': success}

    def _handle_code_required(self, data):
        steamid = data.get('steamid')
        code_type = data.get('code_type', 'unknown')

        logger.warning(f"需要验证码 - SteamID: {steamid}, 类型: {code_type}")

        return {'status': 'ok', 'message': 'Code required callback received'}

    def _handle_order_callback(self, data):
        logger.info("处理订单回调")
        order_id = data.get('order_id')
        status = data.get('status')

        logger.info(f"订单状态更新 - OrderID: {order_id}, Status: {status}")

        if status == 'completed':
            logger.info(f"订单 {order_id} 已完成")
        elif status == 'failed':
            logger.error(f"订单 {order_id} 失败")
        elif status == 'pending':
            logger.info(f"订单 {order_id} 处理中")

        return {'status': 'ok', 'order_id': order_id, 'received_status': status}

    def _handle_trade_callback(self, data):
        logger.info("处理交易回调")
        trade_id = data.get('trade_id')
        status = data.get('status')

        logger.info(f"交易状态更新 - TradeID: {trade_id}, Status: {status}")

        return {'status': 'ok', 'trade_id': trade_id, 'received_status': status}

    def _handle_email_callback(self, data):
        logger.info("处理邮件回调")
        steamid = data.get('steamid')
        email_type = data.get('email_type')
        sent = data.get('sent', False)

        logger.info(f"邮件发送状态 - SteamID: {steamid}, Type: {email_type}, Sent: {sent}")

        return {'status': 'ok', 'message': 'Email callback received'}

    def _handle_generic_callback(self, data):
        logger.info("处理通用回调")
        return {'status': 'ok', 'received': True}

    def _send_response(self, status_code, data):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer(('0.0.0.0', PORT), CallbackHandler)
    logger.info(f"SteamDT 回调服务已启动")
    logger.info(f"监听地址: http://0.0.0.0:{PORT}")
    logger.info(f"支持的路径:")
    logger.info(f"  GET  /steamdt_verify.txt - 域名验证")
    logger.info(f"  GET  /health - 健康检查")
    logger.info(f"  POST /callback - 通用回调")
    logger.info(f"  POST /callback/authenticator - 认证器回调")
    logger.info(f"  POST /callback/order - 订单回调")
    logger.info(f"  POST /callback/trade - 交易回调")
    logger.info(f"  POST /callback/email - 邮件回调")
    logger.info(f"日志文件: callback_server.log")
    logger.info(f"按 Ctrl+C 停止服务")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务正在停止...")
        server.server_close()
        logger.info("服务已停止")


if __name__ == '__main__':
    main()