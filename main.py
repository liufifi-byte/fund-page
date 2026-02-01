import akshare as ak
import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import re
import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 智能映射表
FUND_PROXIES = {
    '000834': 'gb_qqq',
    '501018': 'hf_CL',
    '014673': 'sz159729',
    '006327': 'sh513050',
    '000216': 'hf_GC',
    '162411': 'gb_xop',
    '006355': 'sh513660',
    '022327': 'sh510880',
    '025857': 'sz159338',
}

FUND_INFO_CACHE = {} 

def format_sina_symbol(code):
    code = str(code).strip()
    if code.startswith(('sh', 'sz', 'hk', 'gb_', 'hf_')): return code
    if len(code) == 5: return f"hk{code}"
    if code.startswith(('51', '56', '58', '6', '9')): return f"sh{code}"
    if code.startswith(('15', '0', '3')): return f"sz{code}"
    if code.startswith(('4', '8')): return f"bj{code}"
    return f"sz{code}"

def parse_sina_response(symbol, data_str, name_fallback, weight):
    try:
        content = data_str.split('="')[1].split('";')[0]
        parts = content.split(',')
        result = {"name": name_fallback, "price": 0.0, "prev": 0.0, "change": 0.0, "weight": weight}

        if symbol.startswith(('sh', 'sz', 'hk')):
            if len(parts) > 3:
                if symbol.startswith('hk'):
                    current = float(parts[6])
                    prev = float(parts[3])
                    result['name'] = parts[1]
                else:
                    current = float(parts[3])
                    prev = float(parts[2])
                    result['name'] = parts[0]
                if current == 0: current = prev
                result['price'] = current
                result['prev'] = prev

        elif symbol.startswith('gb_'):
            if len(parts) > 26:
                result['price'] = float(parts[1])
                result['prev'] = float(parts[26])
                result['name'] = parts[0]

        elif symbol.startswith('hf_'):
            if len(parts) > 0:
                current = float(parts[0])
                prev = float(parts[7]) if len(parts) > 7 else current
                if prev == 0: prev = current
                result['price'] = current
                result['prev'] = prev
                result['name'] = symbol

        if result['prev'] > 0:
            result['change'] = round(((result['price'] - result['prev']) / result['prev']) * 100, 2)
            return result
    except: pass
    return None

def fetch_sina_quotes(tasks):
    if not tasks: return []
    symbols = [t[0] for t in tasks]
    url = f"http://hq.sinajs.cn/list={','.join(symbols)}"
    headers = {"Referer": "http://finance.sina.com.cn"}
    details = []
    try:
        res = requests.get(url, headers=headers, timeout=2)
        if res.status_code == 200:
            task_map = {t[0]: (t[1], t[2]) for t in tasks}
            for line in res.text.strip().split('\n'):
                if '="' not in line: continue
                match = re.search(r'hq_str_(.*?)=', line)
                if match:
                    sym = match.group(1)
                    if sym in task_map:
                        data = parse_sina_response(sym, line, task_map[sym][0], task_map[sym][1])
                        if data: details.append(data)
    except: pass
    return details

def get_fund_info_fast(code):
    if code in FUND_INFO_CACHE: return FUND_INFO_CACHE[code]
    
    url = f"http://fund.eastmoney.com/pingzhongdata/{code}.js"
    # ⚠️ 关键修改：默认 position 为 None (表示未获取到)
    result = {"name": f"基金 {code}", "position": None, "pos_type": "股票", "report_date": ""}

    try:
        headers = { "User-Agent": "Mozilla/5.0" }
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200:
            text = res.text
            name_match = re.search(r'fS_name\s*=\s*"(.*?)"', text)
            if name_match: result["name"] = name_match.group(1)

            # ⚠️ 关键修改：正则兼容带引号的数字 "95.5"
            # 原来的正则: \["(.*?)",([\d\.]+),
            # 现在的正则: \["(.*?)",\s*"?([\d\.]+)"?,
            pos_match = re.search(r'var Data_assetAllocation\s*=\s*(\[.*?\]);', text, re.DOTALL)
            if pos_match:
                try:
                    last_item = re.findall(r'\["(.*?)",\s*"?([\d\.]+)"?,', pos_match.group(1))[-1]
                    result["report_date"] = last_item[0]
                    result["position"] = float(last_item[1])
                    if "联接" in result["name"] or "FOF" in result["name"]: result["pos_type"] = "基金"
                    else: result["pos_type"] = "股票"
                    print(f"   📊 {code} 抓取成功: {result['position']}%")
                except Exception as e: 
                    print(f"   ⚠️ {code} 正则解析失败: {e}")
            else:
                print(f"   ⚠️ {code} 没找到 Data_assetAllocation")
            
            FUND_INFO_CACHE[code] = result
    except: pass
    return result

@app.get("/fund/{code}")
def get_fund_estimate(code: str, benchmark: str = None):
    print(f"=== 查询: {code} ===")
    
    fund_info = get_fund_info_fast(code)
    
    # 逻辑拆分：
    # 1. calc_pos: 用于数学计算 (如果没抓到，默认95，保证能算出数)
    # 2. display_pos: 用于前端展示 (如果没抓到，就是 None，前端不显示)
    real_pos = fund_info['position']
    calc_pos = real_pos if real_pos is not None else 95.0
    display_pos = real_pos # 可能是 None

    if (not benchmark) and (code in FUND_PROXIES):
        benchmark = FUND_PROXIES[code]

    try:
        # --- 对标模式 ---
        if benchmark and benchmark.strip():
            sina_symbol = format_sina_symbol(benchmark)
            quote_data = fetch_sina_quotes([(sina_symbol, f"对标:{benchmark}", 100)])
            
            if quote_data:
                quote = quote_data[0]
                final = quote['change'] * (calc_pos / 100.0)
                return {
                    "status": "success",
                    "fund_code": code,
                    "fund_name": fund_info['name'], 
                    "fund_position": display_pos, # 传给前端 (可能是 None)
                    "pos_type": fund_info['pos_type'],
                    "position_date": fund_info['report_date'],
                    "estimate_change": f"{round(final, 2):+}%",
                    "raw_value": round(final, 2),
                    "data_time": datetime.datetime.now().strftime("%m-%d %H:%M"),
                    "details": [quote]
                }

        # --- 持仓模式 ---
        df_hold = ak.fund_portfolio_hold_em(symbol=code)
        if df_hold.empty:
            return {"status": "failed", "error": "无持仓数据"}

        df_hold = df_hold.head(10)
        tasks = []
        for _, row in df_hold.iterrows():
            c_col = next(c for c in df_hold.columns if '代码' in c)
            w_col = next(c for c in df_hold.columns if '比例' in c)
            n_col = next(c for c in df_hold.columns if '名称' in c)
            sina_sym = format_sina_symbol(str(row[c_col]).strip())
            w = float(row[w_col])
            tasks.append((sina_sym, row[n_col], w))

        details = fetch_sina_quotes(tasks)
        total_p = 0
        valid_w = 0
        for d in details:
            total_p += d['change'] * d['weight']
            valid_w += d['weight']

        if valid_w > 0:
            raw = total_p / valid_w
            final = raw * (calc_pos / 100.0)
            details.sort(key=lambda x: x['weight'], reverse=True)
            
            return {
                "status": "success",
                "fund_code": code,
                "fund_name": fund_info['name'],
                "fund_position": display_pos, # 传给前端 (可能是 None)
                "pos_type": fund_info['pos_type'],
                "position_date": fund_info['report_date'],
                "estimate_change": f"{round(final, 2):+}%",
                "raw_value": round(final, 2),
                "data_time": datetime.datetime.now().strftime("%m-%d %H:%M"),
                "details": details 
            }
        else:
            return {"status": "failed", "error": "接口无响应"}

    except Exception as e:
        return {"status": "failed", "error": str(e)}

if __name__ == "__main__":
# 替换 main.py 最底部的启动代码
if __name__ == "__main__":
    import uvicorn
    import os
    # 获取云服务器分配的端口，如果没有则默认 8000
    # host 必须改成 0.0.0.0 (代表允许外网访问)
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)