from flask import Flask, request, jsonify, g, send_from_directory, session
from flask_cors import CORS
from flask_session import Session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import time
import requests
import re
from datetime import datetime, date, timedelta
import threading
import schedule

# 初始化Flask应用
app = Flask(__name__)
CORS(app, supports_credentials=True)  # 支持跨域+Cookie
app.secret_key = 'FundSystem_2026_Secret_Key_123456'  # 生产环境请修改
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # Session1小时有效期
Session(app)

# 配置项
DATABASE = 'funds.db'  # 数据库文件
STATIC_FOLDER = os.path.join(os.path.dirname(__file__), 'static')  # 前端目录
FUND_API_URL = 'http://fundgz.1234567.com.cn/js/{fund_code}.js?rt={timestamp}'  # 真实基金接口
HEADERS = {  # 请求头，避免接口拦截
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/121.0.0.0 Safari/537.36'
}


# ---------------------- 数据库工具函数（核心：4张表初始化） ----------------------
def get_db():
    """获取数据库连接，返回字典格式行数据"""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row  # 行转字典，方便取值
    return db


@app.teardown_appcontext
def close_connection(exception):
    """请求结束自动关闭数据库连接"""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    """
    严格按要求初始化4张核心表，无冗余
    1. users(用户表) 2. user_fund_relation(用户-基金-本金关系表)
    3. user_fund_earnings(用户-基金-本金-日期-收益表) 4. fund_daily_trend(基金-日期-涨势表)
    """
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        # 1. 用户表
        cur.execute('''
                    CREATE TABLE IF NOT EXISTS users
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        username
                        TEXT
                        NOT
                        NULL
                        UNIQUE,
                        password
                        TEXT
                        NOT
                        NULL,
                        create_time
                        TEXT
                        NOT
                        NULL
                    )
                    ''')
        # 2. 用户-基金-本金关系表（核心关联表，用户+基金唯一）
        cur.execute('''
                    CREATE TABLE IF NOT EXISTS user_fund_relation
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        user_id
                        INTEGER
                        NOT
                        NULL,
                        fund_code
                        TEXT
                        NOT
                        NULL,
                        fund_name
                        TEXT
                        NOT
                        NULL,
                        invest_principal
                        REAL
                        NOT
                        NULL, -- 投入本金
                        add_time
                        TEXT
                        NOT
                        NULL, -- 添加时间（YYYY-MM-DD HH:MM:SS）
                        UNIQUE
                    (
                        user_id,
                        fund_code
                    ), -- 约束：一个用户只能添加一次同一基金
                        FOREIGN KEY
                    (
                        user_id
                    ) REFERENCES users
                    (
                        id
                    )
                        )
                    ''')
        # 3. 用户-基金-本金-日期-收益表（每日收益落库表）
        cur.execute('''
                    CREATE TABLE IF NOT EXISTS user_fund_earnings
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        user_id
                        INTEGER
                        NOT
                        NULL,
                        fund_code
                        TEXT
                        NOT
                        NULL,
                        record_date
                        TEXT
                        NOT
                        NULL, -- 记录日期（YYYY-MM-DD）
                        invest_principal
                        REAL
                        NOT
                        NULL, -- 当日投入本金（同步关系表）
                        day_gszzl
                        REAL
                        NOT
                        NULL, -- 当日涨幅（%）
                        day_earn
                        REAL
                        NOT
                        NULL, -- 当日收益（元）
                        total_earn
                        REAL
                        NOT
                        NULL, -- 截至当日累计收益（元）
                        create_time
                        TEXT
                        NOT
                        NULL,
                        UNIQUE
                    (
                        user_id,
                        fund_code,
                        record_date
                    ), -- 约束：用户-基金-日期唯一
                        FOREIGN KEY
                    (
                        user_id
                    ) REFERENCES users
                    (
                        id
                    )
                        )
                    ''')
        # 4. 基金-日期-涨势表（基金行情落库表，所有用户共享）
        cur.execute('''
                    CREATE TABLE IF NOT EXISTS fund_daily_trend
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        fund_code
                        TEXT
                        NOT
                        NULL,
                        record_date
                        TEXT
                        NOT
                        NULL, -- 记录日期（YYYY-MM-DD）
                        jzrq
                        TEXT
                        NOT
                        NULL, -- 净值日期
                        dwjz
                        REAL
                        NOT
                        NULL, -- 单位净值
                        gsz
                        REAL
                        NOT
                        NULL, -- 估值净值
                        gszzl
                        REAL
                        NOT
                        NULL, -- 当日涨幅（%）
                        gztime
                        TEXT
                        NOT
                        NULL, -- 估值更新时间
                        create_time
                        TEXT
                        NOT
                        NULL,
                        UNIQUE
                    (
                        fund_code,
                        record_date
                    ) -- 约束：基金-日期唯一
                        )
                    ''')
        # 插入测试用户（admin/123456 | test/123456），密码加密
        cur.execute('SELECT * FROM users WHERE username=?', ('admin',))
        if not cur.fetchone():
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            cur.execute('''
                        INSERT INTO users (username, password, create_time)
                        VALUES (?, ?, ?),
                               (?, ?, ?)
                        ''', (
                            'admin', generate_password_hash('123456'), now,
                            'test', generate_password_hash('123456'), now
                        ))
        db.commit()
        print("✅ 数据库初始化完成，严格创建4张指定表，插入测试用户")


# ---------------------- 登录校验装饰器 ----------------------
def login_required(f):
    """接口登录校验，未登录返回401"""

    def wrapper(*args, **kwargs):
        if 'user_id' not in session or 'username' not in session:
            return jsonify({'code': 401, 'msg': '未登录，请先登录', 'data': None}), 401
        return f(*args, **kwargs)

    wrapper.__name__ = f.__name__
    return wrapper


# ---------------------- 基金接口工具（解析JSONP、拉取实时数据） ----------------------
def fetch_fund_real(fund_code):
    """
    拉取真实基金接口数据，解析JSONP格式
    :param fund_code: 基金代码（如004253）
    :return: 解析后字典/None（失败）
    """
    try:
        timestamp = int(time.time())
        url = FUND_API_URL.format(fund_code=fund_code, timestamp=timestamp)
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        # 解析JSONP：匹配jsonpgz({...})中的内容
        content = res.text.strip()
        match = re.search(r'jsonpgz\((\{.*\})\);', content)
        if not match:
            print(f"❌ 基金{fund_code}：接口返回非标准JSONP")
            return None
        # 数据类型转换（字符串→浮点数）
        import json
        fund_data = json.loads(match.group(1))
        fund_data['dwjz'] = float(fund_data['dwjz']) if fund_data['dwjz'] else 0.0
        fund_data['gsz'] = float(fund_data['gsz']) if fund_data['gsz'] else 0.0
        fund_data['gszzl'] = float(fund_data['gszzl']) if fund_data['gszzl'] else 0.0
        return fund_data
    except Exception as e:
        print(f"❌ 基金{fund_code}：拉取失败 - {str(e)}")
        return None


# ---------------------- 定时任务（每日15:30落库行情+收益数据） ----------------------
def calculate_day_earn(user_id, fund_code, record_date, gszzl):
    """
    计算单基金单用户当日收益+累计收益
    :return: (当日收益, 截至当日累计收益)
    """
    db = get_db()
    cur = db.cursor()
    # 1. 获取用户该基金的投入本金
    cur.execute('''
                SELECT invest_principal
                FROM user_fund_relation
                WHERE user_id = ?
                  AND fund_code = ?
                ''', (user_id, fund_code))
    relation = cur.fetchone()
    if not relation:
        return (0.0, 0.0)
    invest_principal = relation['invest_principal']
    # 2. 当日收益 = 投入本金 × 涨幅（百分比转小数）
    day_earn = round(invest_principal * (float(gszzl) / 100), 2)
    # 3. 累计收益 = 昨日累计收益 + 当日收益（无昨日数据则为当日收益）
    yesterday = (datetime.strptime(record_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    cur.execute('''
                SELECT total_earn
                FROM user_fund_earnings
                WHERE user_id = ?
                  AND fund_code = ?
                  AND record_date = ?
                ''', (user_id, fund_code, yesterday))
    yesterday_data = cur.fetchone()
    total_earn = round((yesterday_data['total_earn'] if yesterday_data else 0) + day_earn, 2)
    return (day_earn, total_earn)


def auto_record_data():
    """
    定时落库核心逻辑（每日15:30执行）
    1. 拉取所有用户已添加基金的实时行情，落库到fund_daily_trend
    2. 计算每个用户每只基金的当日收益，落库到user_fund_earnings
    """
    with app.app_context():
        try:
            db = get_db()
            cur = db.cursor()
            # 获取所有用户-基金关联数据（去重基金）
            cur.execute('SELECT DISTINCT ufr.user_id, ufr.fund_code, ufr.invest_principal FROM user_fund_relation ufr')
            user_fund_list = cur.fetchall()
            if not user_fund_list:
                print("ℹ️ 定时任务：暂无用户添加基金，无需落库")
                return
            today = date.today().strftime("%Y-%m-%d")
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            success = 0
            fail = 0

            for item in user_fund_list:
                user_id = item['user_id']
                fund_code = item['fund_code']
                # 1. 拉取实时行情数据
                fund_data = fetch_fund_real(fund_code)
                if not fund_data:
                    fail += 1
                    continue
                gszzl = fund_data['gszzl']
                # 行情落库（fund_daily_trend），避免重复
                cur.execute('SELECT * FROM fund_daily_trend WHERE fund_code=? AND record_date=?', (fund_code, today))
                if not cur.fetchone():
                    cur.execute('''
                                INSERT INTO fund_daily_trend (fund_code, record_date, jzrq, dwjz, gsz, gszzl, gztime, create_time)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''', (
                                    fund_code, today, fund_data['jzrq'], fund_data['dwjz'],
                                    fund_data['gsz'], gszzl, fund_data['gztime'], now
                                ))
                # 2. 计算并落库收益数据（user_fund_earnings）
                day_earn, total_earn = calculate_day_earn(user_id, fund_code, today, gszzl)
                cur.execute('SELECT * FROM user_fund_earnings WHERE user_id=? AND fund_code=? AND record_date=?',
                            (user_id, fund_code, today))
                if not cur.fetchone():
                    cur.execute('''
                                INSERT INTO user_fund_earnings (user_id, fund_code, record_date, invest_principal,
                                                                day_gszzl, day_earn, total_earn, create_time)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''', (
                                    user_id, fund_code, today, item['invest_principal'],
                                    gszzl, day_earn, total_earn, now
                                ))
                success += 1
            db.commit()
            print(f"✅ 定时任务完成：成功落库{success}条，失败{fail}条（{today}）")
        except Exception as e:
            print(f"❌ 定时任务失败：{str(e)}")


def start_schedule():
    """启动定时任务守护线程，不阻塞Flask主进程"""
    schedule.every().day.at("15:30").do(auto_record_data)

    # 开发测试：每分钟执行，上线注释
    # schedule.every(1).minutes.do(auto_record_data)
    def run_schedule():
        while True:
            schedule.run_pending()
            time.sleep(60)

    t = threading.Thread(target=run_schedule, daemon=True)
    t.start()
    print("🚀 定时任务启动：每日15:30自动落库基金行情+收益数据")


# ---------------------- 核心计算工具（收益/本金/涨幅，严格按需求） ----------------------
def get_fund_add_date(user_id, fund_code):
    """获取基金添加日期（YYYY-MM-DD），用于筛选历史收益"""
    db = get_db()
    cur = db.cursor()
    cur.execute('''
                SELECT add_time
                FROM user_fund_relation
                WHERE user_id = ?
                  AND fund_code = ?
                ''', (user_id, fund_code))
    add_time = cur.fetchone()['add_time']
    return add_time.split(' ')[0]


def calc_history_earn_sum(user_id, fund_code):
    """计算基金添加日至今的历史收益之和（不含今日，今日为实时计算）"""
    db = get_db()
    cur = db.cursor()
    add_date = get_fund_add_date(user_id, fund_code)
    today = date.today().strftime("%Y-%m-%d")
    cur.execute('''
                SELECT SUM(day_earn) as sum_earn
                FROM user_fund_earnings
                WHERE user_id = ?
                  AND fund_code = ?
                  AND record_date >= ?
                  AND record_date < ?
                ''', (user_id, fund_code, add_date, today))
    sum_earn = cur.fetchone()['sum_earn'] or 0.0
    return round(sum_earn, 2)


def calc_total_earn(user_id, fund_code, today_earn):
    """累计收益 = 历史收益之和 + 今日实时收益（需求3）"""
    history_earn = calc_history_earn_sum(user_id, fund_code)
    total_earn = round(history_earn + today_earn, 2)
    return total_earn


def calc_current_principal(invest_principal, total_earn):
    """现存本金 = 投入本金 + 累计收益（需求4）"""
    return round(invest_principal + total_earn, 2)


def calc_today_earn(current_principal, today_gszzl):
    """今日收益 = 现存本金 × 今日实时涨幅（需求5）"""
    today_earn = round(current_principal * (float(today_gszzl) / 100), 2)
    return today_earn


# ---------------------- 前端页面路由 ----------------------
@app.route('/')
def serve_frontend():
    """根路径返回前端页面"""
    return send_from_directory(STATIC_FOLDER, 'index.html')


# ---------------------- 用户接口（登录/登出/当前用户） ----------------------
@app.route('/api/login', methods=['POST'])
def login():
    """用户登录，验证后设置Session"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        if not username or not password:
            return jsonify({'code': 400, 'msg': '账号/密码不能为空', 'data': None})
        # 验证用户
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT id, username, password FROM users WHERE username=?', (username,))
        user = cur.fetchone()
        if not user or not check_password_hash(user['password'], password):
            return jsonify({'code': 403, 'msg': '账号/密码错误', 'data': None})
        # 设置Session
        session['user_id'] = user['id']
        session['username'] = user['username']
        return jsonify({
            'code': 200, 'msg': '登录成功',
            'data': {'user_id': user['id'], 'username': user['username']}
        })
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'登录失败：{str(e)}', 'data': None})


@app.route('/api/logout', methods=['GET'])
def logout():
    """登出，清除Session"""
    session.clear()
    return jsonify({'code': 200, 'msg': '登出成功', 'data': None})


@app.route('/api/current-user', methods=['GET'])
def current_user():
    """获取当前登录用户"""
    if 'user_id' in session and 'username' in session:
        return jsonify({
            'code': 200, 'msg': '获取成功',
            'data': {'user_id': session['user_id'], 'username': session['username']}
        })
    return jsonify({'code': 401, 'msg': '未登录', 'data': None})


# ---------------------- 基金核心接口（增删改查+刷新+饼图+趋势图） ----------------------
@app.route('/api/fund/query/<fund_code>', methods=['GET'])
@login_required
def fund_query(fund_code):
    """新增基金前的实时查询，验证基金是否存在"""
    try:
        fund_data = fetch_fund_real(fund_code)
        if not fund_data:
            return jsonify({'code': 404, 'msg': '基金不存在或接口拉取失败', 'data': None})
        return jsonify({'code': 200, 'msg': '查询成功', 'data': fund_data})
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'查询失败：{str(e)}', 'data': None})


@app.route('/api/fund', methods=['POST'])
@login_required
def fund_add():
    """新增基金，添加到关系表，同时首次落库当日行情+收益"""
    try:
        data = request.get_json()
        fund_code = data.get('fundcode', '').strip()
        invest_principal = round(float(data.get('invest_principal', 0.0)), 2)
        # 参数校验
        if not fund_code or invest_principal <= 0:
            return jsonify({'code': 400, 'msg': '基金代码不能为空，本金必须大于0', 'data': None})
        # 验证基金存在
        fund_data = fetch_fund_real(fund_code)
        if not fund_data:
            return jsonify({'code': 404, 'msg': '基金不存在，无法添加', 'data': None})
        # 校验是否已添加
        db = get_db()
        cur = db.cursor()
        cur.execute('''
                    SELECT *
                    FROM user_fund_relation
                    WHERE user_id = ?
                      AND fund_code = ?
                    ''', (session['user_id'], fund_code))
        if cur.fetchone():
            return jsonify({'code': 409, 'msg': '已添加该基金，无需重复添加', 'data': None})
        # 1. 添加到用户-基金关系表
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cur.execute('''
                    INSERT INTO user_fund_relation (user_id, fund_code, fund_name, invest_principal, add_time)
                    VALUES (?, ?, ?, ?, ?)
                    ''', (session['user_id'], fund_code, fund_data['name'], invest_principal, now))
        # 2. 首次落库当日行情（fund_daily_trend）
        today = date.today().strftime("%Y-%m-%d")
        cur.execute('SELECT * FROM fund_daily_trend WHERE fund_code=? AND record_date=?', (fund_code, today))
        if not cur.fetchone():
            cur.execute('''
                        INSERT INTO fund_daily_trend (fund_code, record_date, jzrq, dwjz, gsz, gszzl, gztime, create_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            fund_code, today, fund_data['jzrq'], fund_data['dwjz'],
                            fund_data['gsz'], fund_data['gszzl'], fund_data['gztime'], now
                        ))
        # 3. 首次落库当日收益（user_fund_earnings）
        day_earn, total_earn = calculate_day_earn(session['user_id'], fund_code, today, fund_data['gszzl'])
        cur.execute('SELECT * FROM user_fund_earnings WHERE user_id=? AND fund_code=? AND record_date=?',
                    (session['user_id'], fund_code, today))
        if not cur.fetchone():
            cur.execute('''
                        INSERT INTO user_fund_earnings (user_id, fund_code, record_date, invest_principal, day_gszzl,
                                                        day_earn, total_earn, create_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            session['user_id'], fund_code, today, invest_principal,
                            fund_data['gszzl'], day_earn, total_earn, now
                        ))
        db.commit()
        return jsonify({
            'code': 200, 'msg': '基金添加成功',
            'data': {'fund_code': fund_code, 'fund_name': fund_data['name'], 'invest_principal': invest_principal}
        })
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'添加失败：{str(e)}', 'data': None})


@app.route('/api/fund/list', methods=['GET'])
@login_required
def fund_list():
    """
    获取我的基金数据列表（核心接口，严格按需求计算所有字段）
    返回字段：投入本金、累计收益、现存本金、昨日涨幅/收益、今日涨幅/收益
    """
    try:
        db = get_db()
        cur = db.cursor()
        user_id = session['user_id']
        # 获取用户所有基金关系数据
        cur.execute('''
                    SELECT *
                    FROM user_fund_relation
                    WHERE user_id = ?
                    ORDER BY add_time DESC
                    ''', (user_id,))
        relation_list = cur.fetchall()
        if not relation_list:
            return jsonify({'code': 200, 'msg': '暂无基金数据', 'data': []})

        fund_list = []
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        for relation in relation_list:
            fund_code = relation['fund_code']
            invest_principal = relation['invest_principal']
            fund_name = relation['fund_name']
            add_time = relation['add_time']
            # 1. 拉取今日实时行情数据
            today_real = fetch_fund_real(fund_code) or {}
            today_gszzl = today_real.get('gszzl', 0.0)
            today_dwjz = today_real.get('dwjz', 0.0)
            today_gztime = today_real.get('gztime', '')
            # 2. 计算今日实时收益（需求5）
            # 先临时计算今日收益（无累计收益时，今日收益=投入本金×今日涨幅）
            temp_today_earn = calc_today_earn(invest_principal, today_gszzl)
            # 3. 计算累计收益（需求3）= 历史收益之和 + 今日实时收益
            history_earn_sum = calc_history_earn_sum(user_id, fund_code)
            total_earn = calc_total_earn(user_id, fund_code, temp_today_earn)
            # 4. 计算现存本金（需求4）= 投入本金 + 累计收益
            current_principal = calc_current_principal(invest_principal, total_earn)
            # 5. 重新计算今日收益（用最终的现存本金，需求5）
            today_earn = calc_today_earn(current_principal, today_gszzl)
            # 6. 重新计算累计收益（确保精准）
            total_earn = calc_total_earn(user_id, fund_code, today_earn)
            # 7. 获取昨日涨幅/收益（从历史收益表取）
            cur.execute('''
                        SELECT day_gszzl, day_earn
                        FROM user_fund_earnings
                        WHERE user_id = ?
                          AND fund_code = ?
                          AND record_date = ?
                        ''', (user_id, fund_code, yesterday))
            yesterday_data = cur.fetchone() or {}
            yesterday_gszzl = yesterday_data.get('day_gszzl', 0.0)
            yesterday_earn = yesterday_data.get('day_earn', 0.0)
            # 组装数据
            fund_list.append({
                'fund_code': fund_code,
                'fund_name': fund_name,
                'invest_principal': invest_principal,  # 投入本金
                'total_earn': total_earn,  # 累计收益
                'current_principal': current_principal,  # 现存本金
                'yesterday_gszzl': yesterday_gszzl,  # 昨日涨幅
                'yesterday_earn': yesterday_earn,  # 昨日收益
                'today_gszzl': today_gszzl,  # 今日涨幅
                'today_earn': today_earn,  # 今日收益
                'today_dwjz': today_dwjz,
                'today_gztime': today_gztime,
                'add_time': add_time
            })
        return jsonify({'code': 200, 'msg': '获取成功', 'data': fund_list})
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'获取失败：{str(e)}', 'data': []})


@app.route('/api/fund/<fund_code>', methods=['DELETE'])
@login_required
def fund_delete(fund_code):
    """删除基金，同时删除关联的收益数据"""
    try:
        db = get_db()
        cur = db.cursor()
        user_id = session['user_id']
        # 校验基金是否属于当前用户
        cur.execute('''
                    SELECT *
                    FROM user_fund_relation
                    WHERE user_id = ?
                      AND fund_code = ?
                    ''', (user_id, fund_code))
        if not cur.fetchone():
            return jsonify({'code': 404, 'msg': '基金不存在', 'data': None})
        # 删除关系表+收益表数据（行情表共享，不删除）
        cur.execute('DELETE FROM user_fund_relation WHERE user_id=? AND fund_code=?', (user_id, fund_code))
        cur.execute('DELETE FROM user_fund_earnings WHERE user_id=? AND fund_code=?', (user_id, fund_code))
        db.commit()
        return jsonify({'code': 200, 'msg': '删除成功', 'data': fund_code})
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'删除失败：{str(e)}', 'data': None})


@app.route('/api/fund/<fund_code>/principal', methods=['PUT'])
@login_required
def fund_update_principal(fund_code):
    """修改基金投入本金"""
    try:
        data = request.get_json()
        new_principal = round(float(data.get('invest_principal', 0.0)), 2)
        if new_principal <= 0:
            return jsonify({'code': 400, 'msg': '本金必须大于0', 'data': None})
        db = get_db()
        cur = db.cursor()
        user_id = session['user_id']
        # 校验基金归属
        cur.execute('''
                    SELECT *
                    FROM user_fund_relation
                    WHERE user_id = ?
                      AND fund_code = ?
                    ''', (user_id, fund_code))
        if not cur.fetchone():
            return jsonify({'code': 404, 'msg': '基金不存在', 'data': None})
        # 更新本金
        cur.execute('''
                    UPDATE user_fund_relation
                    SET invest_principal=?
                    WHERE user_id = ?
                      AND fund_code = ?
                    ''', (new_principal, user_id, fund_code))
        db.commit()
        return jsonify({
            'code': 200, 'msg': '本金修改成功',
            'data': {'fund_code': fund_code, 'new_invest_principal': new_principal}
        })
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'修改失败：{str(e)}', 'data': None})


@app.route('/api/fund/chart/pie', methods=['GET'])
@login_required
def fund_chart_pie():
    """
    新增：获取饼图数据（需求1）
    1. 本金比例饼图：各基金投入本金/总投入
    2. 今日收益比例饼图：各基金今日收益/总今日收益
    """
    """
        修复：空数据兜底，返回含「暂无数据」的饼图数据，避免前端空白
        1. 本金比例饼图：各基金投入本金/总投入
        2. 今日收益比例饼图：各基金今日收益/总今日收益
        """
    try:
        db = get_db()
        cur = db.cursor()
        user_id = session['user_id']
        # 获取基金关系数据
        cur.execute('SELECT * FROM user_fund_relation WHERE user_id=?', (user_id,))
        relation_list = cur.fetchall()

        # ---------- 新增：空数据兜底 ----------
        if not relation_list:
            return jsonify({
                'code': 200, 'msg': '暂无饼图数据',
                'data': {
                    # 兜底数据：让饼图显示「暂无数据」，值为1（避免空白）
                    'principal_pie': [{'name': '暂无数据', 'value': 1, 'pct': 100.0}],
                    'today_earn_pie': [{'name': '暂无数据', 'value': 1, 'pct': 100.0}]
                }
            })
        # ---------- 空数据兜底结束 ----------

        # 初始化数据
        principal_pie = []  # 本金比例饼图
        today_earn_pie = []  # 今日收益比例饼图
        total_invest = 0.0  # 总投入本金
        total_today_earn = 0.0  # 总今日收益

        # 关键修复：将sqlite3.Row对象转换为可修改的字典，并存储今日收益
        relation_dict_list = []
        # 先计算总投入和总今日收益
        for relation in relation_list:
            # 将Row对象转为字典，支持赋值操作
            relation_dict = dict(relation)
            fund_code = relation_dict['fund_code']
            invest_principal = relation_dict['invest_principal']
            total_invest += invest_principal

            # 计算今日收益
            today_real = fetch_fund_real(fund_code) or {}
            today_gszzl = today_real.get('gszzl', 0.0)
            history_earn = calc_history_earn_sum(user_id, fund_code)
            temp_today_earn = calc_today_earn(invest_principal, today_gszzl)
            total_earn = calc_total_earn(user_id, fund_code, temp_today_earn)
            current_principal = calc_current_principal(invest_principal, total_earn)
            today_earn = calc_today_earn(current_principal, today_gszzl)

            # 现在可以安全赋值，因为是字典对象
            relation_dict['today_earn'] = today_earn
            total_today_earn += today_earn
            relation_dict_list.append(relation_dict)

        # 组装饼图数据（保留2位小数）
        for relation in relation_dict_list:
            fund_name = relation['fund_name']
            invest_principal = relation['invest_principal']
            today_earn = relation['today_earn']

            # 本金比例
            principal_pct = round((invest_principal / total_invest) * 100, 2) if total_invest > 0 else 0.0
            principal_pie.append({
                'name': fund_name,
                'value': round(invest_principal, 2),
                'pct': principal_pct
            })

            # 今日收益比例（总收益为0时比例为0）
            earn_pct = round((today_earn / total_today_earn) * 100, 2) if abs(total_today_earn) > 0 else 0.0
            today_earn_pie.append({
                'name': fund_name,
                'value': round(today_earn, 2),
                'pct': earn_pct
            })

        return jsonify({
            'code': 200, 'msg': '获取饼图数据成功',
            'data': {'principal_pie': principal_pie, 'today_earn_pie': today_earn_pie}
        })
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'获取饼图数据失败：{str(e)}', 'data': None})


@app.route('/api/fund/chart/trend/<fund_code>', methods=['GET'])
@login_required
def fund_chart_trend(fund_code):
    """
    新增：获取基金趋势图数据（需求6）
    返回：添加后分天的日期、收益、涨幅，用于折线图
    """
    try:
        db = get_db()
        cur = db.cursor()
        user_id = session['user_id']
        # 校验基金是否属于当前用户
        cur.execute('''
                    SELECT *
                    FROM user_fund_relation
                    WHERE user_id = ?
                      AND fund_code = ?
                    ''', (user_id, fund_code))
        relation = cur.fetchone()
        if not relation:
            return jsonify({'code': 404, 'msg': '基金不存在', 'data': None})
        # 获取基金添加日期
        add_date = get_fund_add_date(user_id, fund_code)
        today = date.today().strftime("%Y-%m-%d")
        # 查询添加日至今的收益数据（含涨幅、收益）
        cur.execute('''
                    SELECT record_date, day_gszzl, day_earn, total_earn
                    FROM user_fund_earnings
                    WHERE user_id = ?
                      AND fund_code = ?
                      AND record_date >= ?
                      AND record_date <= ?
                    ORDER BY record_date ASC
                    ''', (user_id, fund_code, add_date, today))
        trend_data = cur.fetchall()
        if not trend_data:
            return jsonify({'code': 200, 'msg': '暂无趋势数据（添加后未到统计时间）', 'data': []})
        # 组装趋势图数据（适配前端折线图）
        result = []
        for item in trend_data:
            result.append({
                'date': item['record_date'],
                'gszzl': round(item['day_gszzl'], 2),  # 当日涨幅
                'day_earn': round(item['day_earn'], 2),  # 当日收益
                'total_earn': round(item['total_earn'], 2)  # 累计收益
            })
        # 补充今日实时数据（如果今日数据未落库）
        last_date = result[-1]['date']
        if last_date != today:
            today_real = fetch_fund_real(fund_code) or {}
            today_gszzl = round(today_real.get('gszzl', 0.0), 2)
            # 计算今日实时收益
            invest_principal = relation['invest_principal']
            history_earn = calc_history_earn_sum(user_id, fund_code)
            temp_today_earn = calc_today_earn(invest_principal, today_gszzl)
            total_earn = calc_total_earn(user_id, fund_code, temp_today_earn)
            current_principal = calc_current_principal(invest_principal, total_earn)
            today_earn = round(calc_today_earn(current_principal, today_gszzl), 2)
            result.append({
                'date': today,
                'gszzl': today_gszzl,
                'day_earn': today_earn,
                'total_earn': round(total_earn, 2)
            })
        return jsonify({
            'code': 200, 'msg': '获取趋势图数据成功',
            'data': {'fund_name': relation['fund_name'], 'trend_list': result}
        })
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'获取趋势图数据失败：{str(e)}', 'data': None})


@app.route('/api/fund/refresh', methods=['GET'])
@login_required
def fund_refresh():
    """
    刷新数据接口（需求2：替换手动记录）
    无实际落库，仅触发全量数据重新计算，返回刷新成功
    """
    try:
        # 主动拉取一个基金数据触发接口请求，验证接口连通性（可选，核心是前端重新加载）
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT fund_code FROM user_fund_relation WHERE user_id=? LIMIT 1', (session['user_id'],))
        fund_code = cur.fetchone()
        if fund_code:
            fetch_fund_real(fund_code['fund_code'])  # 拉取实时数据，更新缓存
        return jsonify({'code': 200, 'msg': '数据刷新成功', 'data': None})
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'刷新失败：{str(e)}', 'data': None})


@app.route('/api/fund/stat', methods=['GET'])
@login_required
def fund_stat():
    """获取基金总统计数据（概览卡片：总投入、总现存、总今日收益、总累计收益）"""
    try:
        db = get_db()
        cur = db.cursor()
        user_id = session['user_id']
        cur.execute('SELECT * FROM user_fund_relation WHERE user_id=?', (user_id,))
        relation_list = cur.fetchall()
        if not relation_list:
            return jsonify({
                'code': 200, 'msg': '暂无统计数据',
                'data': {
                    'total_invest': 0.0, 'total_current': 0.0,
                    'total_today_earn': 0.0, 'total_total_earn': 0.0
                }
            })
        # 计算总统计数据
        total_invest = 0.0
        total_current = 0.0
        total_today_earn = 0.0
        total_total_earn = 0.0
        for relation in relation_list:
            fund_code = relation['fund_code']
            invest_principal = relation['invest_principal']
            total_invest += invest_principal
            # 计算单基金各项数据
            today_real = fetch_fund_real(fund_code) or {}
            today_gszzl = today_real.get('gszzl', 0.0)
            history_earn = calc_history_earn_sum(user_id, fund_code)
            temp_today_earn = calc_today_earn(invest_principal, today_gszzl)
            total_earn = calc_total_earn(user_id, fund_code, temp_today_earn)
            current_principal = calc_current_principal(invest_principal, total_earn)
            today_earn = calc_today_earn(current_principal, today_gszzl)
            # 累加总数据
            total_current += current_principal
            total_today_earn += today_earn
            total_total_earn += total_earn
        # 保留2位小数
        total_invest = round(total_invest, 2)
        total_current = round(total_current, 2)
        total_today_earn = round(total_today_earn, 2)
        total_total_earn = round(total_total_earn, 2)
        return jsonify({
            'code': 200, 'msg': '获取统计数据成功',
            'data': {
                'total_invest': total_invest,  # 总投入本金
                'total_current': total_current,  # 总现存本金
                'total_today_earn': total_today_earn,  # 总今日收益
                'total_total_earn': total_total_earn  # 总累计收益
            }
        })
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'获取统计数据失败：{str(e)}', 'data': None})


# ---------------------- 启动应用 ----------------------
if __name__ == '__main__':
    init_db()  # 初始化数据库
    start_schedule()  # 启动定时任务
    # 创建static目录（如果不存在）
    if not os.path.exists(STATIC_FOLDER):
        os.makedirs(STATIC_FOLDER)
    print("🚀 基金管理系统启动成功！")
    print("🔗 访问地址：http://127.0.0.1:5000")
    print("🔑 测试账号：admin/123456 | test/123456")
    print(
        "⚡ 刷新数据接口：/api/fund/refresh | 饼图接口：/api/fund/chart/pie | 趋势图接口：/api/fund/chart/trend/[基金代码]")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)