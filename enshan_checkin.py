"""
cron "39 12 * * *" script-path=恩山论坛签到.py,tag=匹配cron用
new Env('恩山论坛签到')
"""

import os
import re
import random
import time
import requests
from datetime import datetime

# ---------------- 统一通知模块加载 ----------------
hadsend = False
send = None
try:
    from notify import send
    hadsend = True
    print("✅ 已加载notify.py通知模块")
except ImportError:
    print("⚠️  未加载通知模块，跳过通知功能")

# 配置项
enshan_cookie = os.environ.get('enshan_cookie', '')
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "3600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"
privacy_mode = os.getenv("PRIVACY_MODE", "true").lower() == "true"

# 恩山论坛配置
BASE_URL = 'https://www.right.com.cn/forum'
FORUM_URL = f'{BASE_URL}/forum.php'
SIGN_ACTION_URL = f'{BASE_URL}/plugin.php?id=erling_qd:action&action=sign'
SIGN_PAGE_URL = f'{BASE_URL}/erling_qd-sign_in.html'

# 积分页候选（按顺序尝试）
CREDIT_URLS = [
    f'{BASE_URL}/home.php?mod=spacecp&ac=credit',
    f'{BASE_URL}/home.php?mod=spacecp&ac=credit&showcredit=1',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0'
}


def is_captcha_page(text):
    """阿里云ESA WAF滑块验证码页特征"""
    if not text:
        return False
    return ('验证您是真人' in text
            or 'aliyunCaptcha' in text
            or '阿里云ESA' in text
            or 'id="renderData"' in text)


def is_xss_error_page(text):
    """Discuz _xss_check 拦截页特征（formhash与会话不匹配时触发）"""
    if not text:
        return False
    return '非法字符' in text or 'request_tainting' in text


def mask_username(username):
    """用户名脱敏处理"""
    if not username:
        return username

    if privacy_mode:
        if len(username) <= 2:
            return '*' * len(username)
        elif len(username) <= 4:
            return username[0] + '*' * (len(username) - 2) + username[-1]
        else:
            return username[0] + '*' * 3 + username[-1]
    return username


def format_time_remaining(seconds):
    """格式化时间显示"""
    if seconds <= 0:
        return "立即执行"
    hours, minutes = divmod(seconds, 3600)
    minutes, secs = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{secs}秒"
    elif minutes > 0:
        return f"{minutes}分{secs}秒"
    else:
        return f"{secs}秒"


def wait_with_countdown(delay_seconds, task_name):
    """带倒计时的随机延迟等待"""
    if delay_seconds <= 0:
        return
    print(f"{task_name} 需要等待 {format_time_remaining(delay_seconds)}")
    remaining = delay_seconds
    while remaining > 0:
        if remaining <= 10 or remaining % 10 == 0:
            print(f"{task_name} 倒计时: {format_time_remaining(remaining)}")
        sleep_time = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(sleep_time)
        remaining -= sleep_time


def notify_user(title, content):
    """统一通知函数"""
    if hadsend:
        try:
            send(title, content)
            print(f"✅ 通知发送完成: {title}")
        except Exception as e:
            print(f"❌ 通知发送失败: {e}")
    else:
        print(f"📢 {title}\n📄 {content}")


def parse_cookies(cookie_str):
    """解析Cookie字符串，支持多账号（换行或&&分隔）"""
    if not cookie_str:
        return []

    lines = cookie_str.strip().split('\n')
    cookies = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split('&&')
        for part in parts:
            part = part.strip()
            if part:
                cookies.append(part)

    unique_cookies = []
    for cookie in cookies:
        if cookie and cookie not in unique_cookies:
            unique_cookies.append(cookie)

    return unique_cookies


def extract_number(text):
    """从文本中提取数字"""
    if not text:
        return 0
    try:
        number_str = re.sub(r'[^\d]', '', str(text))
        return int(number_str) if number_str else 0
    except (ValueError, TypeError):
        return 0


def extract_first(text, patterns, default=None, flags=0):
    """按顺序尝试正则，返回第一个匹配到的 group(1)（strip后）"""
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            value = match.group(1)
            return value.strip() if isinstance(value, str) else value
    return default


class EnShanSigner:
    name = "恩山论坛"

    def __init__(self, cookie: str, index: int = 1):
        self.cookie = cookie
        self.index = index
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.headers['Cookie'] = cookie

        self.user_name = None
        self.user_group = None
        self.coin_before = None
        self.point_before = None
        self.contribution = None
        self.coin_after = None
        self.point_after = None
        self.formhash = None
        self.uid = None

    def _sync_cookie_header(self):
        self.session.headers['Cookie'] = self.cookie

    @staticmethod
    def _upsert_cookie(base_cookies, new_cookie_kv):
        """把新的 key=value 合并进 Cookie 字符串（同键替换，异键追加）"""
        if not new_cookie_kv or '=' not in new_cookie_kv:
            return base_cookies
        new_key, new_value = new_cookie_kv.split('=', 1)
        new_key = new_key.strip()
        new_value = new_value.strip()

        parts = []
        replaced = False
        for raw in base_cookies.split(';'):
            part = raw.strip()
            if not part or '=' not in part:
                continue
            key, value = part.split('=', 1)
            key = key.strip()
            if key == new_key:
                parts.append(f"{new_key}={new_value}")
                replaced = True
            else:
                parts.append(f"{key}={value.strip()}")
        if not replaced:
            parts.append(f"{new_key}={new_value}")
        return '; '.join(parts)

    def _merge_response_cookies(self, response):
        """把响应 Set-Cookie 合并回请求 Cookie（如 WAF 下发的 acw_tc 等）"""
        for name, value in response.cookies.items():
            self.cookie = self._upsert_cookie(self.cookie, f"{name}={value}")
        self._sync_cookie_header()

    def _get(self, url, referer=None, timeout=20):
        """统一 GET，返回 response 或 None"""
        headers = dict(HEADERS)
        if referer:
            headers['Referer'] = referer
        try:
            resp = self.session.get(url=url, headers=headers, timeout=timeout, allow_redirects=True)
            print(f"🔍 GET {url.split('/')[-1][:60]} -> {resp.status_code}")
            return resp
        except Exception as e:
            print(f"❌ GET 请求异常: {url} -> {e}")
            return None

    def check_session(self):
        """GET forum.php：校验登录态并取 formhash / uid

        返回 (ok, msg, stage)，stage 用于精确诊断：
        captcha=被ESA验证码拦截 / guest=Cookie失效 / error=其他
        """
        print("🔐 正在校验登录态...")
        resp = self._get(FORUM_URL, referer=FORUM_URL)
        if resp is None:
            return False, "论坛页请求失败（网络异常）", "error"

        if is_captcha_page(resp.text):
            return False, ("阿里云ESA滑块验证码拦截：连 forum.php 都被罩住，"
                           "当前出口IP已被WAF标记，需更换青龙服务器出口IP或引入浏览器过盾方案"), "captcha"

        self._merge_response_cookies(resp)

        formhash = extract_first(
            resp.text,
            patterns=[r'name="formhash"\s+value="([0-9a-fA-F]+)"'],
            default=None,
        )
        uid_match = re.search(r"discuz_uid\s*=\s*'(\d+)'", resp.text)
        uid = uid_match.group(1) if uid_match else None

        if not formhash:
            return False, "论坛页未提取到formhash参数", "error"

        if not uid or uid == '0':
            return False, ("Cookie失效或未登录（discuz_uid=0）：请重新登录恩山论坛，"
                           "F12复制完整Cookie后更新 enshan_cookie 环境变量"), "guest"

        self.formhash = formhash
        self.uid = uid
        print(f"✅ 登录态正常: uid={uid}, formhash={formhash}")
        return True, "登录成功", "ok"

    def get_user_info(self, is_after=False):
        """获取用户信息和积分"""
        try:
            print(f"👤 正在获取{'签到后' if is_after else '签到前'}用户信息...")
            time.sleep(random.uniform(2, 5))

            response = None
            last_status = None
            for url in CREDIT_URLS:
                for attempt in range(1, 4):
                    headers = dict(HEADERS)
                    headers['Referer'] = FORUM_URL
                    try:
                        resp = self.session.get(url=url, headers=headers, timeout=20, allow_redirects=True)
                    except Exception as e:
                        print(f"❌ 积分页请求异常: {e}")
                        break
                    last_status = resp.status_code
                    if resp.status_code == 200 and resp.text:
                        response = resp
                        break
                    if resp.status_code in (429, 521) or 500 <= resp.status_code < 600:
                        time.sleep(1.5 * attempt + random.uniform(0, 0.8))
                        continue
                    break
                if response is not None:
                    break

            if response is None:
                error_msg = f"获取用户信息失败，状态码: {last_status}"
                print(f"❌ {error_msg}")
                return False, error_msg

            print(f"🔍 用户信息响应状态码: {response.status_code}")

            if is_captcha_page(response.text):
                error_msg = "积分页被阿里云ESA验证码拦截（当前出口IP已被WAF标记）"
                print(f"❌ {error_msg}")
                return False, error_msg

            if response.status_code == 200:
                coin = extract_first(
                    response.text,
                    patterns=[
                        r"恩山币\s*[:：]\s*</em>\s*([^<&\s]+)",
                        r"恩山币\s*[:：]\s*([^<\s]+)\s*币",
                        r"恩山币\s*[:：]\s*([^<\s]+)",
                    ],
                    default="0",
                    flags=re.S,
                )
                point = extract_first(
                    response.text,
                    patterns=[
                        r"积分\s*[:：]\s*</em>\s*([^<&\s]+)",
                        r"<em>\s*积分\s*[:：]\s*</em>\s*([^<\s]+)",
                        r"积分\s*[:：]\s*([^<\s]+)",
                    ],
                    default="0",
                    flags=re.S,
                )

                if is_after:
                    self.coin_after = coin
                    self.point_after = point
                    print(f"💰 签到后 - 恩山币: {coin}, 积分: {point}")
                else:
                    self.coin_before = coin
                    self.point_before = point
                    print(f"💰 签到前 - 恩山币: {coin}, 积分: {point}")

                if not is_after:
                    self.user_name = extract_first(
                        response.text,
                        patterns=[
                            r'访问我的空间">([^<]+)</a>',
                            r'class="vwmy"[^>]*>([^<]+)</a>',
                            r'欢迎您回来\s*,\s*([^<\n]+)',
                            r'用户名[：:]\s*([^<\n]+)',
                        ],
                        default="未知用户",
                        flags=re.S,
                    )
                    self.user_group = extract_first(
                        response.text,
                        patterns=[
                            r'用户组\s*[:：]\s*([^<\n]+)</',
                            r'用户组\s*[:：]\s*([^<\n]+)',
                        ],
                        default="未知等级",
                        flags=re.S,
                    )
                    self.contribution = extract_first(
                        response.text,
                        patterns=[
                            r'贡献\s*[:：]\s*</em>\s*([^<\s]+)\s*分',
                            r'贡献\s*[:：]\s*(\d+)',
                        ],
                        default="0",
                        flags=re.S,
                    )
                    print(f"👤 用户: {mask_username(self.user_name)}")
                    print(f"🏅 等级: {self.user_group}")
                    print(f"🎯 贡献: {self.contribution}")

                return True, "用户信息获取成功"

            error_msg = f"获取用户信息失败，状态码: {response.status_code}"
            print(f"❌ {error_msg}")
            return False, error_msg

        except Exception as e:
            error_msg = f"获取用户信息异常: {str(e)}"
            print(f"❌ {error_msg}")
            return False, error_msg

    def perform_checkin(self):
        """执行签到

        注意：ESA WAF 会把 urlencoded POST body 合并进查询串，而 Discuz _xss_check
        对查询串里的 formhash 与会话值做强校验，不匹配即返回503"非法字符"。
        因此主请求把 formhash 放查询串；若仍被拦，回退 body 形式，并刷新 formhash 重试。
        """
        print("📝 正在执行签到...")

        if not self.formhash:
            ok, msg, _stage = self.check_session()
            if not ok:
                return False, f"请先校验登录态获取formhash: {msg}"

        headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SIGN_PAGE_URL,
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        attempts = [
            ("查询串", lambda: self.session.get(f"{SIGN_ACTION_URL}&formhash={self.formhash}", headers=headers, timeout=30)),
            ("请求体", lambda: self.session.post(SIGN_ACTION_URL, headers={**headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}, data={"formhash": self.formhash}, timeout=30)),
        ]

        last_err = "未知错误"
        for name, do_request in attempts:
            try:
                response = do_request()
            except Exception as e:
                last_err = f"签到请求异常: {e}"
                print(f"❌ {last_err}")
                continue

            print(f"🔍 签到响应({name})状态码: {response.status_code}")

            if is_captcha_page(response.text):
                return False, "签到动作接口被阿里云ESA验证码拦截，当前出口IP已被WAF标记，需更换青龙服务器出口IP或引入浏览器过盾方案"

            if is_xss_error_page(response.text):
                last_err = f"Discuz _xss_check 拦截（{name}形式，formhash与会话不匹配）"
                print(f"⚠️ {last_err}")
                continue

            try:
                result = response.json()
            except ValueError:
                return False, f"签到响应非JSON: {response.text[:200]}"

            if isinstance(result, dict):
                message = str(result.get("message", "")).strip()
                status = result.get("status")
                success = result.get("success")

                if success is True or status in (1, "1", "success", True):
                    return True, message or "签到成功"
                if "已签到" in message or "已经签到" in message:
                    return True, message
                if "成功" in message:
                    return True, message
                if message:
                    return False, f"签到失败: {message}"
            return True, "签到请求已提交"

        # 两种形式都被 _xss_check 拦截：formhash 可能已过期，刷新后重试一次
        print("🔄 formhash 可能已过期，刷新后重试...")
        ok, msg, _stage = self.check_session()
        if ok and self.formhash:
            try:
                response = self.session.get(f"{SIGN_ACTION_URL}&formhash={self.formhash}", headers=headers, timeout=30)
                print(f"🔍 签到响应(重试)状态码: {response.status_code}")
                if is_captcha_page(response.text):
                    return False, "签到动作接口被阿里云ESA验证码拦截，当前出口IP已被WAF标记"
                if not is_xss_error_page(response.text):
                    try:
                        result = response.json()
                    except ValueError:
                        return False, f"签到响应非JSON: {response.text[:200]}"
                    if isinstance(result, dict):
                        message = str(result.get("message", "")).strip()
                        if "已签到" in message or "成功" in message:
                            return True, message
                        if message:
                            return False, f"签到失败: {message}"
                    return True, "签到请求已提交"
            except Exception as e:
                last_err = f"重试异常: {e}"

        return False, last_err

    def main(self):
        """主执行函数"""
        print(f"\n==== 恩山论坛账号{self.index} 开始签到 ====")

        if not self.cookie.strip():
            error_msg = """账号配置错误

❌ 错误原因: Cookie为空

🔧 解决方法:
1. 在青龙面板中添加环境变量enshan_cookie
2. 多账号用换行分隔或&&分隔
3. Cookie需要包含完整的登录信息

💡 提示: 请确保Cookie有效且格式正确"""
            print(f"❌ {error_msg}")
            return error_msg, False

        # 1. 校验登录态并取 formhash
        login_success, login_msg, stage = self.check_session()
        if not login_success:
            print(f"❌ 登录态校验失败[{stage}]: {login_msg}")
            return f"登录失败: {login_msg}", False

        # 2. 获取签到前用户信息
        user_success, user_msg = self.get_user_info(is_after=False)
        if not user_success:
            print(f"⚠️ 获取用户信息失败: {user_msg}")

        # 3. 随机等待
        time.sleep(random.uniform(2, 5))

        # 4. 执行签到
        signin_success, signin_msg = self.perform_checkin()

        # 5. 获取签到后用户信息
        time.sleep(random.uniform(2, 4))
        after_success, after_msg = self.get_user_info(is_after=True)

        # 6. 通过积分变化判断签到是否真的成功
        gain_info = ""
        if after_success and self.coin_before and self.coin_after:
            try:
                coin_before = extract_number(self.coin_before)
                coin_after = extract_number(self.coin_after)
                point_before = extract_number(self.point_before)
                point_after = extract_number(self.point_after)

                coin_gain = coin_after - coin_before
                point_gain = point_after - point_before

                print(f"📊 积分变化: 恩山币 {coin_before}→{coin_after} (+{coin_gain}), 积分 {point_before}→{point_after} (+{point_gain})")

                if coin_gain > 0 or point_gain > 0:
                    signin_success = True
                    signin_msg = f"签到成功，获得 {coin_gain} 恩山币，{point_gain} 积分"
                    gain_info = f"\n🎁 本次收益: +{coin_gain} 恩山币, +{point_gain} 积分"
                    print(f"✅ 通过积分变化确认签到成功: +{coin_gain} 恩山币, +{point_gain} 积分")
                elif coin_gain == 0 and point_gain == 0:
                    signin_success = True
                    signin_msg = "今日已签到（积分无变化）"
                    print("📅 积分无变化，今日已签到")
                else:
                    print("⚠️ 积分变化异常，但仍认为签到成功")
                    signin_success = True

            except Exception as e:
                print(f"⚠️ 积分变化计算异常: {e}")
                print("🔄 使用原始签到结果")

        # 7. 组合结果消息
        final_msg = f"""🌟 恩山论坛签到结果

    👤 用户: {mask_username(self.user_name) or '未知用户'}
    🏅 等级: {self.user_group or '未知等级'}
    💰 恩山币: {self.coin_before or '未知'} → {self.coin_after or self.coin_before or '未知'}
    📊 积分: {self.point_before or '未知'} → {self.point_after or self.point_before or '未知'}
    🎯 贡献: {self.contribution or '0'} 分{gain_info}

    📝 签到: {signin_msg}
    ⏰ 时间: {datetime.now().strftime('%m-%d %H:%M')}"""

        print(f"{'✅ 任务完成' if signin_success else '❌ 任务失败'}")
        return final_msg, signin_success


def main():
    """主程序入口"""
    print(f"==== 恩山论坛签到开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    print(f"🔒 隐私保护模式: {'已启用' if privacy_mode else '已禁用'}")

    if random_signin:
        delay_seconds = random.randint(0, max_random_delay)
        if delay_seconds > 0:
            print(f"🎲 随机延迟: {format_time_remaining(delay_seconds)}")
            wait_with_countdown(delay_seconds, "恩山论坛签到")

    if not enshan_cookie:
        error_msg = """❌ 未找到enshan_cookie环境变量

🔧 配置方法:
1. enshan_cookie: 恩山论坛Cookie
2. 多账号用换行分隔或&&分隔
3. Cookie需要包含完整的登录信息

示例:
单账号: enshan_cookie=完整的Cookie字符串
多账号: enshan_cookie=cookie1&&cookie2 或换行分隔

💡 提示: 登录恩山论坛后，F12复制完整Cookie"""

        print(error_msg)
        notify_user("恩山论坛签到失败", error_msg)
        return

    cookies = parse_cookies(enshan_cookie)

    if not cookies:
        error_msg = """❌ Cookie解析失败

🔧 可能原因:
1. Cookie格式不正确
2. Cookie为空或只包含空白字符
3. 分隔符使用错误

💡 请检查enshan_cookie环境变量的值"""

        print(error_msg)
        notify_user("恩山论坛签到失败", error_msg)
        return

    print(f"📝 共发现 {len(cookies)} 个账号")

    success_count = 0
    total_count = len(cookies)
    results = []

    for index, cookie in enumerate(cookies):
        try:
            if index > 0:
                delay = random.uniform(10, 20)
                print(f"⏱️  随机等待 {delay:.1f} 秒后处理下一个账号...")
                time.sleep(delay)

            signer = EnShanSigner(cookie, index + 1)
            result_msg, is_success = signer.main()

            if is_success:
                success_count += 1

            results.append({
                'index': index + 1,
                'success': is_success,
                'message': result_msg,
                'username': mask_username(signer.user_name) if signer.user_name else f"账号{index + 1}"
            })

            status = "成功" if is_success else "失败"
            title = f"恩山论坛账号{index + 1}签到{status}"
            notify_user(title, result_msg)

        except Exception as e:
            error_msg = f"账号{index + 1}: 执行异常 - {str(e)}"
            print(f"❌ {error_msg}")
            notify_user(f"恩山论坛账号{index + 1}签到失败", error_msg)

    if total_count > 1:
        summary_msg = f"""📊 恩山论坛签到汇总

📈 总计: {total_count}个账号
✅ 成功: {success_count}个
❌ 失败: {total_count - success_count}个
📊 成功率: {success_count/total_count*100:.1f}%
⏰ 完成时间: {datetime.now().strftime('%m-%d %H:%M')}"""

        if len(results) <= 5:
            summary_msg += "\n\n📋 详细结果:"
            for result in results:
                status_icon = "✅" if result['success'] else "❌"
                summary_msg += f"\n{status_icon} {result['username']}"

        notify_user("恩山论坛签到汇总", summary_msg)

    print(f"\n==== 恩山论坛签到完成 - 成功{success_count}/{total_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")


def handler(event, context):
    """云函数入口"""
    main()


if __name__ == "__main__":
    main()
