"""Taiwan Stock Desktop Screener - native Tkinter UI, no web server."""
import csv
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import pandas as pd
import requests
import yfinance as yf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib import rcParams
rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft JhengHei UI", "Noto Sans CJK TC", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

APP_NAME = '台股智慧選股 V2.6｜固定型態參數獨立勾選版'
DATA_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TaiwanStockDesktop'
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / 'universe.json'
FEEDS = [('上市', 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L', '.TW'),
         ('上櫃', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O', '.TWO')]
DEMO = [('2330', '台積電', '上市', '2330.TW'), ('2317', '鴻海', '上市', '2317.TW'),
        ('2454', '聯發科', '上市', '2454.TW'), ('2303', '聯電', '上市', '2303.TW')]


def load_universe(force=False):
    today = datetime.now().strftime('%Y-%m-%d')
    cached = {}
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    if not force and cached.get('date') == today and cached.get('rows'):
        return cached['rows'], '使用今日已更新的官方股票清單'
    rows, errors = [], []
    for market, url, suffix in FEEDS:
        try:
            resp = requests.get(url, timeout=25, headers={'User-Agent': 'Mozilla/5.0'})
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict):
                payload = next((x for x in payload.values() if isinstance(x, list)), [])
            count = 0
            for item in payload:
                if not isinstance(item, dict):
                    continue
                code = str(next((item.get(k) for k in ('公司代號', '證券代號', '股票代號', 'SecuritiesCompanyCode', 'Code') if item.get(k)), '')).strip()
                name = str(next((item.get(k) for k in ('公司簡稱', '公司名稱', '證券名稱', '股票名稱', 'CompanyAbbreviation', 'Name') if item.get(k)), '')).strip()
                if re.fullmatch(r'\d{4}', code):
                    rows.append([code, name, market, code + suffix])
                    count += 1
            if count == 0:
                errors.append(f'{market} API 回傳資料欄位不符')
        except Exception as exc:
            errors.append(f'{market}：{str(exc)[:100]}')
    if rows:
        unique = list({(r[0], r[2]): r for r in rows}.values())
        if not errors:
            CACHE_FILE.write_text(json.dumps({'date': today, 'rows': unique}, ensure_ascii=False), encoding='utf-8')
        else:
            # preserve old market when only one API works
            if cached.get('rows'):
                markets = {r[2] for r in unique}
                unique.extend(r for r in cached['rows'] if r[2] not in markets)
        return sorted(unique), '；'.join(errors) if errors else f'官方名冊更新成功（{today}）'
    if cached.get('rows'):
        return cached['rows'], '官方資料更新失敗，使用舊名冊（非最新）：' + '；'.join(errors)
    return [], '官方名冊取得失敗，請確認網路；可使用自選股票：' + '；'.join(errors)


def fetch(symbol):
    df = yf.download(symbol, period='1y', interval='1d', progress=False,
                     auto_adjust=False, threads=False, timeout=16)
    if df is None or df.empty:
        raise ValueError('無歷史行情')
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    needed = ['Open', 'High', 'Low', 'Close', 'Volume']
    df = df[needed].apply(pd.to_numeric, errors='coerce').dropna().sort_index()
    if len(df) < 65:
        raise ValueError('歷史行情不足65日')
    return df


def indicators(df):
    d = df.copy()
    for n in (5, 10, 20, 60):
        d[f'MA{n}'] = d['Close'].rolling(n).mean()
    lo = d['Low'].rolling(9).min()
    hi = d['High'].rolling(9).max()
    rsv = ((d['Close'] - lo) / (hi - lo).replace(0, float('nan')) * 100).fillna(50)
    d['K'] = rsv.ewm(alpha=1/3, adjust=False).mean()
    d['D'] = d['K'].ewm(alpha=1/3, adjust=False).mean()
    ema12 = d['Close'].ewm(span=12, adjust=False).mean()
    ema26 = d['Close'].ewm(span=26, adjust=False).mean()
    d['DIF'] = ema12 - ema26
    d['DEA'] = d['DIF'].ewm(span=9, adjust=False).mean()
    d['MACD'] = (d['DIF'] - d['DEA']) * 2
    delta = d['Close'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d['RSI'] = (100 - 100/(1 + gain/loss.replace(0, float('nan')))).fillna(100)
    return d


def screen(df, opts):
    """Compare latest N trading days against preceding base days.

    A stock is NOT at its chosen lookback's highest *closing* price.
    The lookback excludes the current bar, so a new high today fails.
    """
    n = opts['recent'] if opts['mode'] == '近N日均量' else 1
    base = opts['base']
    high_days = opts['high_days']
    if len(df) < max(65, n + base if opts['use_volume_ratio'] else 0, high_days + 1 if opts['use_not_high'] else 0):
        raise ValueError(f'歷史行情不足（需至少 {max(65, n + base, high_days + 1)} 日）')
    recent_avg = float(df['Volume'].iloc[-n:].mean())
    base_avg = float(df['Volume'].iloc[-(n + base):-n].mean())
    if base_avg <= 0 and opts['use_volume_ratio']:
        raise ValueError('基準成交量為零')
    ratio = recent_avg / base_avg if base_avg > 0 else 0
    # Yahoo Finance Volume is in shares; TW market convention: 1 lot = 1,000 shares.
    volume_lots = float(df['Volume'].iloc[-1]) / 1000.0
    previous_high = float(df['Close'].iloc[-(high_days + 1):-1].max())
    close = float(df['Close'].iloc[-1])
    is_not_highest = close < previous_high
    # MA trend: compare today's selected moving average with the same MA 5 bars ago.
    trend = opts['trend']
    ma_days = opts['trend_days']
    if opts['use_trend'] and trend != '不限制':
        if len(df) < ma_days + 5:
            raise ValueError(f'趨勢判斷至少需要 {ma_days + 5} 個交易日')
        ma_now = float(df['Close'].iloc[-ma_days:].mean())
        ma_prev = float(df['Close'].iloc[-(ma_days + 5):-5].mean())
        slope = (ma_now / ma_prev - 1) if ma_prev else 0
        if trend == '上升' and slope <= 0:
            return False, ratio, previous_high, close, volume_lots
        if trend == '下降' and slope >= 0:
            return False, ratio, previous_high, close, volume_lots
        if trend == '盤整' and abs(slope) > opts['flat_tolerance'] / 100:
            return False, ratio, previous_high, close, volume_lots
    # For multi-day candlestick screening, every bar must satisfy the chosen rule.
    k_rule = opts['k_rule']
    if opts['use_k_rule'] and k_rule != '不限制':
        candles = df.iloc[-opts['k_days']:]
        if k_rule == '全部紅K':
            k_ok = bool((candles['Close'] > candles['Open']).all())
        elif k_rule == '全部黑K':
            k_ok = bool((candles['Close'] < candles['Open']).all())
        else:  # 全部非紅K: includes doji candles
            k_ok = bool((candles['Close'] <= candles['Open']).all())
        if not k_ok:
            return False, ratio, previous_high, close, volume_lots
    return ((not opts['use_volume_ratio'] or ratio >= opts['multiple'])
            and (not opts['use_not_high'] or is_not_highest)
            and (not opts['use_min_volume'] or volume_lots >= opts['min_volume_lots'])), ratio, previous_high, close, volume_lots


def reversal_pattern(df, opts):
    """Find long decline -> dry-up -> breakout; no look-ahead.

    A: latest bar is first qualifying spike after quiet 10 bars.
    B: a spike 1..5 bars ago, followed by price recovery.
    Both classes require decline and dry-up before the spike.
    """
    lookback = opts['decline_days']
    dry_days = opts['dry_days']
    spike_mult = opts['spike_multiple']
    need = max(lookback + 1, dry_days * 2 + 6, 65)
    if len(df) < need:
        raise ValueError(f'反轉型態歷史資料不足（至少{need}日）')
    close = df['Close'].astype(float)
    vol = df['Volume'].astype(float)
    last = len(df) - 1
    labels = []
    metrics = None
    # Class A: today's first spike; B: spike within the previous 5 bars.
    for spike_idx in range(max(0, last - 5), last + 1):
        # Compare price to the close lookback days before the spike.
        if spike_idx < lookback or spike_idx < dry_days * 2:
            continue
        old_price = float(close.iloc[spike_idx - lookback])
        bottom_price = float(close.iloc[spike_idx])
        if old_price <= 0:
            continue
        decline_pct = (old_price - bottom_price) / old_price * 100
        if decline_pct < opts['min_decline_pct']:
            continue
        before = vol.iloc[spike_idx - 2 * dry_days:spike_idx - dry_days]
        quiet = vol.iloc[spike_idx - dry_days:spike_idx]
        old_avg, quiet_avg = float(before.mean()), float(quiet.mean())
        if old_avg <= 0 or quiet_avg <= 0:
            continue
        shrink_pct = (1 - quiet_avg / old_avg) * 100
        if shrink_pct < opts['min_shrink_pct']:
            continue
        spike_vol = float(vol.iloc[spike_idx])
        ratio = spike_vol / quiet_avg
        if ratio < spike_mult:
            continue
        # The preceding 10 days must not have reached the same spike multiple.
        preceding = vol.iloc[max(spike_idx - 10, 0):spike_idx]
        if len(preceding) and bool((preceding >= quiet_avg * spike_mult).any()):
            continue
        # Price must be relatively close to the minimum seen before/at spike.
        low_window = df['Low'].iloc[spike_idx - lookback:spike_idx + 1]
        low_price = float(low_window.min())
        if low_price <= 0:
            continue
        low_distance = (float(close.iloc[last]) / low_price - 1) * 100
        if low_distance > opts['max_low_distance_pct']:
            continue
        if spike_idx == last:
            label = 'A 首次爆量'
        else:
            # Recovery after spike: latest close above spike close and MA5 rising.
            if float(close.iloc[last]) <= bottom_price:
                continue
            ma5_now = float(close.iloc[-5:].mean())
            ma5_then = float(close.iloc[-6:-1].mean())
            if ma5_now <= ma5_then:
                continue
            label = 'B 爆量後反彈'
        if label not in labels:
            labels.append(label)
            metrics = (round(decline_pct, 1), round(shrink_pct, 1),
                       round(ratio, 2), round(low_distance, 1))
    return labels, metrics


PATTERN_NAMES = (
    '① 長期下跌＋低檔爆量',
    '② 長期下跌＋量縮整理＋爆量反彈',
    '③ 長期下跌＋均線翻揚',
    '④ 橫盤整理＋突破爆量',
    '⑤ 長期上漲＋高檔爆量',
)


def fixed_patterns(df, opts):
    """Return independently matched pattern names and metrics for the latest signal.

    The signal may have appeared within the previous N sessions. All calculations
    use only bars up to that signal, preventing look-ahead bias.
    """
    selected = opts['fixed_selected']
    if not selected:
        return [], None
    lookback = opts['decline_days']
    dry = opts['dry_days']
    window = opts['signal_days']
    required = max(lookback + 6, 2 * dry + 12, 65)
    if len(df) < required:
        raise ValueError(f'固定型態至少需要{required}個交易日')
    hits = set()
    best = None
    close = df['Close'].astype(float)
    vol = df['Volume'].astype(float)
    high = df['High'].astype(float)
    low = df['Low'].astype(float)
    for idx in range(max(required - 1, len(df) - window), len(df)):
        price = float(close.iloc[idx]); past = float(close.iloc[idx - lookback])
        if past <= 0 or price <= 0:
            continue
        decline = (past - price) / past * 100
        advance = (price / past - 1) * 100
        low_point = float(low.iloc[idx - lookback:idx + 1].min())
        high_point = float(high.iloc[idx - lookback:idx + 1].max())
        low_distance = (price / low_point - 1) * 100 if low_point > 0 else float('inf')
        high_distance = (1 - price / high_point) * 100 if high_point > 0 else float('inf')
        quiet = float(vol.iloc[idx - dry:idx].mean())
        earlier = float(vol.iloc[idx - 2 * dry:idx - dry].mean())
        shrink = (1 - quiet / earlier) * 100 if earlier > 0 else -float('inf')
        ratio = float(vol.iloc[idx]) / quiet if quiet > 0 else 0
        spike = ratio >= opts['spike_multiple']
        down = decline >= opts['min_decline_pct']
        low_enough = low_distance <= opts['max_low_distance_pct']
        dry_enough = shrink >= opts['min_shrink_pct']
        ma5 = float(close.iloc[idx-4:idx+1].mean())
        ma5prev = float(close.iloc[idx-5:idx].mean())
        ma10 = float(close.iloc[idx-9:idx+1].mean())
        ma10prev = float(close.iloc[idx-10:idx].mean())
        labels = []
        if 0 in selected and down and low_enough and spike:
            labels.append(PATTERN_NAMES[0])
        if 1 in selected and down and low_enough and dry_enough and spike and price > float(close.iloc[idx-1]) and ma5 > ma5prev:
            labels.append(PATTERN_NAMES[1])
        # Turn-up: MA5/MA10 both rising now, and at least one was non-rising 1..5 bars ago.
        if 2 in selected and down and low_enough and ma5 > ma5prev and ma10 > ma10prev:
            turning = any(
                float(close.iloc[j-4:j+1].mean()) <= float(close.iloc[j-5:j].mean()) or
                float(close.iloc[j-9:j+1].mean()) <= float(close.iloc[j-10:j].mean())
                for j in range(idx-5, idx) if j >= 10
            )
            if turning:
                labels.append(PATTERN_NAMES[2])
        # Sideways: previous 20 closes have <=15% high/low range; latest close
        # exceeds their highest close, with volume spike.
        previous20 = close.iloc[idx-20:idx]
        if 3 in selected and len(previous20) == 20:
            sideways = float(previous20.max()) / max(float(previous20.min()), .000001) - 1 <= .15
            if sideways and price > float(previous20.max()) and spike:
                labels.append(PATTERN_NAMES[3])
        if 4 in selected and advance >= opts['min_decline_pct'] and high_distance <= 10 and spike:
            labels.append(PATTERN_NAMES[4])
        if labels:
            hits.update(labels)
            best = (round(decline, 1), round(shrink, 1), round(ratio, 2), round(low_distance, 1))
    return [name for name in PATTERN_NAMES if name in hits], best


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry('1720x940')
        root.minsize(1050, 680)
        self.q = queue.Queue()
        self.universe = []
        self.results = []
        self.busy = False
        self.stop_event = threading.Event()
        self.setup()
        self.root.after(120, self.poll)
        self.refresh_universe(False)

    def setup(self):
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=APP_NAME, font=('Microsoft JhengHei UI', 17, 'bold')).pack(anchor='w')
        self.market = tk.StringVar(value='全部')
        self.custom = tk.StringVar(value='')
        self.mode = tk.StringVar(value='當日量')
        self.base = tk.StringVar(value='20')
        self.recent = tk.StringVar(value='3')
        self.multiple = tk.StringVar(value='2.0')
        self.high_days = tk.StringVar(value='40')
        self.min_volume_lots = tk.StringVar(value='1000')
        self.trend = tk.StringVar(value='上升')
        self.trend_days = tk.StringVar(value='20')
        self.flat_tolerance = tk.StringVar(value='1.0')
        self.k_rule = tk.StringVar(value='全部紅K')
        self.k_days = tk.StringVar(value='1')
        self.pattern = tk.StringVar(value='一般選股')
        self.fixed_vars = [tk.BooleanVar(value=False) for _ in PATTERN_NAMES]
        self.signal_days = tk.StringVar(value='3')
        self.decline_days = tk.StringVar(value='120')
        self.min_decline_pct = tk.StringVar(value='30')
        self.dry_days = tk.StringVar(value='20')
        self.min_shrink_pct = tk.StringVar(value='50')
        self.spike_multiple = tk.StringVar(value='3')
        self.max_low_distance_pct = tk.StringVar(value='50')
        self.use_volume_ratio = tk.BooleanVar(value=False)
        self.use_min_volume = tk.BooleanVar(value=False)
        self.use_not_high = tk.BooleanVar(value=False)
        self.use_trend = tk.BooleanVar(value=False)
        self.use_k_rule = tk.BooleanVar(value=False)
        self.use_reversal = tk.BooleanVar(value=False)
        # Dedicated parameters for fixed-pattern scanning; never read the general filters.
        self.fx_decline_days = tk.StringVar(value='120')
        self.fx_min_decline_pct = tk.StringVar(value='30')
        self.fx_dry_days = tk.StringVar(value='20')
        self.fx_min_shrink_pct = tk.StringVar(value='50')
        self.fx_spike_multiple = tk.StringVar(value='3')
        self.fx_max_low_distance_pct = tk.StringVar(value='50')
        self.fx_min_volume_lots = tk.StringVar(value='不限')
        self.fx_use_min_volume = tk.BooleanVar(value=False)
        self.fx_switches = {k: tk.BooleanVar(value=False) for k in ('signal_days','decline_days','min_decline_pct','dry_days','min_shrink_pct','spike_multiple','max_low_distance_pct')}
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill='x', pady=5)
        general = ttk.Frame(self.tabs, padding=6)
        fixed = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(general, text='一般條件選股（各項可勾選）')
        self.tabs.add(fixed, text='固定圖形辨識（完全獨立）')
        common = ttk.Frame(frame); common.pack(fill='x', pady=3)
        self.combo(common, '股票市場（兩模式共用）', self.market, ['全部','上市','上櫃','自選'])
        self.entry(common, '自選股票代號', self.custom, 30)
        r = ttk.Frame(general); r.pack(fill='x', pady=3)
        ttk.Checkbutton(r, text='啟用成交量倍數', variable=self.use_volume_ratio).pack(side='left')
        self.combo(r, '量能模式', self.mode, ['當日量','近N日均量'])
        self.combo(r, '基準天數', self.base, ['20','40','60','80'])
        self.combo(r, '近期N天', self.recent, ['1','2','3','4','5'])
        self.entry(r, '倍數≥', self.multiple, 6)
        r = ttk.Frame(general); r.pack(fill='x', pady=3)
        ttk.Checkbutton(r, text='啟用最低成交量', variable=self.use_min_volume).pack(side='left')
        self.combo(r, '張數≥', self.min_volume_lots, ['不限','1000','2000','3000','4000','5000'])
        ttk.Checkbutton(r, text='啟用非最高收盤', variable=self.use_not_high).pack(side='left', padx=12)
        self.combo(r, '觀察天數', self.high_days, ['20','40','60','80','100','120','140'])
        r = ttk.Frame(general); r.pack(fill='x', pady=3)
        ttk.Checkbutton(r, text='啟用MA趨勢', variable=self.use_trend).pack(side='left')
        self.combo(r, '方向', self.trend, ['上升','下降','盤整'])
        self.combo(r, 'MA天數', self.trend_days, ['5','10','20','40','60'])
        self.entry(r, '盤整±%', self.flat_tolerance, 6)
        ttk.Checkbutton(r, text='啟用K線顏色', variable=self.use_k_rule).pack(side='left', padx=12)
        self.combo(r, '條件', self.k_rule, ['全部紅K','全部黑K','全部非紅K'])
        self.combo(r, '天數', self.k_days, ['1','2','3','4','5'])
        r = ttk.Frame(general); r.pack(fill='x', pady=3)
        ttk.Checkbutton(r, text='改用A/B/C反轉策略（不與上述一般條件交集）', variable=self.use_reversal).pack(side='left')
        self.combo(r, '策略', self.pattern, ['A 首次爆量','B 爆量後反彈','C 兩種都找'])
        r = ttk.Frame(general); r.pack(fill='x', pady=3)
        self.combo(r, '下跌觀察日', self.decline_days, ['40','60','80','100','120','140'])
        self.combo(r, '最低跌幅%', self.min_decline_pct, ['10','20','30','40','50'])
        self.combo(r, '低量期', self.dry_days, ['10','20','40'])
        self.combo(r, '量縮至少%', self.min_shrink_pct, ['30','50','70'])
        self.combo(r, '爆量倍數≥', self.spike_multiple, ['2','3','4','5','8','10'])
        self.combo(r, '距低點≤%', self.max_low_distance_pct, ['20','30','50','80','100'])
        ttk.Label(general, text='不勾選的條件完全不參與一般掃描。若全不勾選，會要求至少啟用一項。').pack(anchor='w')
        r = ttk.Frame(fixed); r.pack(fill='x', pady=3)
        for i, name in enumerate(PATTERN_NAMES):
            ttk.Checkbutton(r, text=name, variable=self.fixed_vars[i]).pack(side='left', padx=4)
        r = ttk.Frame(fixed); r.pack(fill='x', pady=3)
        ttk.Label(r, text='勾選後才覆蓋型態內建預設值：').pack(side='left')
        r = ttk.Frame(fixed); r.pack(fill='x', pady=3)
        for key, label, var, choices in [
            ('signal_days','訊號最近(日)',self.signal_days,['1','3','5','10']),
            ('decline_days','觀察日',self.fx_decline_days,['40','60','80','100','120','140']),
            ('min_decline_pct','最低漲跌幅%',self.fx_min_decline_pct,['10','20','30','40','50']),
            ('dry_days','低量期(日)',self.fx_dry_days,['10','20','40'])]:
            ttk.Checkbutton(r, text='啟用'+label, variable=self.fx_switches[key]).pack(side='left', padx=(6,0))
            self.combo(r, '', var, choices)
        r = ttk.Frame(fixed); r.pack(fill='x', pady=3)
        for key, label, var, choices in [
            ('min_shrink_pct','量縮至少%',self.fx_min_shrink_pct,['30','50','70']),
            ('spike_multiple','爆量倍數≥',self.fx_spike_multiple,['2','3','4','5','8','10']),
            ('max_low_distance_pct','距低點≤%',self.fx_max_low_distance_pct,['20','30','50','80','100'])]:
            ttk.Checkbutton(r, text='啟用'+label, variable=self.fx_switches[key]).pack(side='left', padx=(6,0))
            self.combo(r, '', var, choices)
        ttk.Checkbutton(r, text='啟用最低成交量', variable=self.fx_use_min_volume).pack(side='left', padx=8)
        self.combo(r, '張數≥', self.fx_min_volume_lots, ['不限','1000','2000','3000','4000','5000'])
        ttk.Label(fixed, text='未勾選的參數不會套用畫面上的數值，而是採用型態內建基準：訊號5日、觀察120日、漲跌30%、低量20日、量縮30%、爆量2倍、距低點50%。').pack(anchor='w')
        ttk.Label(fixed, text='型態仍需符合本身的必要價格與量能特徵；最低成交量未勾選則完全不限制。兩分頁條件互不影響。').pack(anchor='w')
        bar = ttk.Frame(frame); bar.pack(fill='x', pady=5)
        self.update_btn = ttk.Button(bar, text='更新上市／上櫃名冊', command=lambda: self.refresh_universe(True)); self.update_btn.pack(side='left', padx=3)
        self.scan_btn = ttk.Button(bar, text='掃描目前分頁', command=self.scan); self.scan_btn.pack(side='left', padx=3)
        ttk.Button(bar, text='停止掃描', command=self.stop_event.set).pack(side='left', padx=3)
        ttk.Button(bar, text='匯出CSV', command=self.export).pack(side='left', padx=3)
        ttk.Button(bar, text='依代號查詢技術線', command=self.lookup).pack(side='left', padx=3)
        self.status = tk.StringVar(value='準備就緒')
        ttk.Label(frame, textvariable=self.status, foreground='#174e7a').pack(anchor='w')
        self.progress = ttk.Progressbar(frame, mode='determinate'); self.progress.pack(fill='x', pady=(2, 5))
        split = ttk.Panedwindow(frame, orient='vertical'); split.pack(fill='both', expand=True)
        top = ttk.Frame(split); bottom = ttk.Frame(split)
        split.add(top, weight=2); split.add(bottom, weight=3)
        cols = ('code', 'name', 'market', 'close', 'volume_lots', 'ratio', 'high', 'date', 'pattern', 'decline', 'shrink', 'spike', 'low_distance')
        self.tree = ttk.Treeview(top, columns=cols, show='headings', height=9)
        for key, label, width in [('code','代號',80),('name','名稱',140),('market','市場',65),('close','收盤價',100),('volume_lots','最新成交量(張)',115),('ratio','放量倍數',110),('high','前N日最高收盤',125),('date','行情日期',130),('pattern','型態分類',130),('decline','歷史跌幅%',100),('shrink','量縮%',90),('spike','爆量倍數',95),('low_distance','距低點%',90)]:
            self.tree.heading(key, text=label); self.tree.column(key, width=width, anchor='center')
        sb = ttk.Scrollbar(top, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True); sb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self.selected)
        ttk.Label(top, text='點選篩選結果的股票，即可查看下方技術線圖；行情下載失敗會顯示於掃描紀錄。').pack(anchor='w')
        self.fig = Figure(figsize=(11, 7), dpi=92)
        self.canvas = FigureCanvasTkAgg(self.fig, master=bottom)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)
        ttk.Label(frame, text='資料來源：TWSE／TPEx 官方名冊、Yahoo Finance 歷史日線。資料可能延遲或缺漏；僅供研究，不構成投資建議。', foreground='gray').pack(anchor='w', pady=3)
        self.cursor = 0

    def combo(self, parent, label, variable, values):
        ttk.Label(parent, text=label).pack(side='left', padx=(9, 2))
        ttk.Combobox(parent, textvariable=variable, values=values, state='readonly', width=11).pack(side='left')

    def entry(self, parent, label, variable, width):
        ttk.Label(parent, text=label).pack(side='left', padx=(9, 2))
        ttk.Entry(parent, textvariable=variable, width=width).pack(side='left')

    def start_task(self, task):
        if self.busy:
            messagebox.showinfo('請稍候', '目前正在執行上一項工作。')
            return False
        self.busy = True
        self.stop_event.clear()
        threading.Thread(target=task, daemon=True).start()
        return True

    def refresh_universe(self, force):
        self.status.set('正在取得上市／上櫃官方股票清單…')
        def job():
            try:
                rows, info = load_universe(force)
                self.q.put(('universe', rows, info))
            except Exception as e:
                self.q.put(('error', str(e)))
            finally:
                self.q.put(('done',))
        self.start_task(job)

    def get_options(self):
        options = dict(use_volume_ratio=self.use_volume_ratio.get(), use_min_volume=self.use_min_volume.get(),
                       use_not_high=self.use_not_high.get(), use_trend=self.use_trend.get(),
                       use_k_rule=self.use_k_rule.get(), use_reversal=self.use_reversal.get(),
                       mode=self.mode.get(), base=int(self.base.get()), recent=int(self.recent.get()),
                       multiple=float(self.multiple.get()), high_days=int(self.high_days.get()),
                       min_volume_lots=0 if self.min_volume_lots.get() == '不限' else int(self.min_volume_lots.get()),
                       trend=self.trend.get(), trend_days=int(self.trend_days.get()),
                       flat_tolerance=float(self.flat_tolerance.get()),
                       k_rule=self.k_rule.get(), k_days=int(self.k_days.get()),
                       pattern=self.pattern.get(), decline_days=int(self.decline_days.get()),
                       min_decline_pct=float(self.min_decline_pct.get()), dry_days=int(self.dry_days.get()),
                       min_shrink_pct=float(self.min_shrink_pct.get()),
                       spike_multiple=float(self.spike_multiple.get()),
                       max_low_distance_pct=float(self.max_low_distance_pct.get()),
                       fixed_selected=[i for i,v in enumerate(self.fixed_vars) if v.get()],
                       signal_days=int(self.signal_days.get()))
        if options['multiple'] <= 0:
            raise ValueError('放量倍數必須大於0')
        if options['flat_tolerance'] < 0:
            raise ValueError('盤整容許幅度不可小於0')
        return options

    def get_pool(self):
        if self.market.get() == '自選':
            codes = re.findall(r'\d{4,6}', self.custom.get())
            known = {r[0]: r for r in self.universe}
            return [known.get(c, [c, c, '自選', c+'.TW']) for c in dict.fromkeys(codes)]
        return [r for r in self.universe if self.market.get() == '全部' or r[2] == self.market.get()]

    def scan(self):
        if self.busy:
            messagebox.showinfo('請稍候', '目前仍在執行其他工作。')
            return
        try:
            opts = self.get_options()
            opts['scan_mode'] = 'fixed' if self.tabs.index(self.tabs.select()) == 1 else 'general'
            if opts['scan_mode'] == 'fixed':
                if not opts['fixed_selected']:
                    raise ValueError('請先勾選至少一種固定型態。')
                # Unchecked controls retain independent built-in pattern criteria.
                # They do NOT read or inherit values from the general-filter tab.
                defaults = dict(signal_days=5, decline_days=120, min_decline_pct=30.0,
                                dry_days=20, min_shrink_pct=30.0, spike_multiple=2.0,
                                max_low_distance_pct=50.0)
                controls = dict(signal_days=self.signal_days, decline_days=self.fx_decline_days,
                                min_decline_pct=self.fx_min_decline_pct, dry_days=self.fx_dry_days,
                                min_shrink_pct=self.fx_min_shrink_pct, spike_multiple=self.fx_spike_multiple,
                                max_low_distance_pct=self.fx_max_low_distance_pct)
                for key, default in defaults.items():
                    opts[key] = (type(default)(controls[key].get())
                                 if self.fx_switches[key].get() else default)
                opts['min_volume_lots'] = (0 if not self.fx_use_min_volume.get() or
                                            self.fx_min_volume_lots.get() == '不限'
                                            else int(self.fx_min_volume_lots.get()))
            elif not opts['use_reversal'] and not any(opts[k] for k in ('use_volume_ratio','use_min_volume','use_not_high','use_trend','use_k_rule')):
                raise ValueError('一般選股請至少勾選一個條件。')
            pool = self.get_pool()
        except Exception as exc:
            messagebox.showerror('設定錯誤', str(exc))
            return
        if not pool:
            messagebox.showwarning('無股票', '請先更新名冊，或改用自選股票。')
            return
        self.results.clear()
        self.cursor = 0
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.progress['maximum'] = len(pool)
        self.progress['value'] = 0
        self.status.set(f"準備執行{'固定型態' if opts['scan_mode']=='fixed' else '一般條件'}掃描，共 {len(pool)} 檔…")

        def job():
            failed = 0
            reasons = {}
            matches = 0
            done_count = 0
            try:
                for idx, (code, name, market, symbol) in enumerate(pool):
                    if self.stop_event.is_set():
                        break
                    self.q.put(('progress', idx + 1, len(pool),
                                f'全市場掃描 {idx + 1}/{len(pool)}：{code} {name}；符合 {matches} 檔；失敗 {failed} 檔'))
                    try:
                        df = fetch(symbol)
                        if opts['scan_mode'] == 'fixed':
                            labels, metrics = fixed_patterns(df, opts)
                            matched = bool(labels)
                            close = float(df['Close'].iloc[-1])
                            volume_lots = float(df['Volume'].iloc[-1]) / 1000
                            matched = matched and volume_lots >= opts['min_volume_lots']
                            highest = float(df['Close'].iloc[-(opts['high_days']+1):-1].max())
                            ratio = metrics[2] if metrics else 0
                        elif not opts['use_reversal']:
                            matched, ratio, highest, close, volume_lots = screen(df, opts)
                            labels, metrics = [], None
                        else:
                            labels, metrics = reversal_pattern(df, opts)
                            matched = bool(labels) and (
                                opts['pattern'] == 'C 兩種都找' or
                                any(x.startswith(opts['pattern'][0]) for x in labels))
                            close = float(df['Close'].iloc[-1])
                            volume_lots = float(df['Volume'].iloc[-1]) / 1000
                            matched = matched and volume_lots >= opts['min_volume_lots']
                            highest = float(df['Close'].iloc[-(opts['high_days']+1):-1].max())
                            ratio = metrics[2] if metrics else 0
                        if matched:
                            matches += 1
                            stats = metrics if metrics else ('', '', '', '')
                            self.q.put(('match', [code, name, market, round(close, 2),
                                                  round(volume_lots, 1), round(ratio, 2), round(highest, 2),
                                                  str(df.index[-1].date()), symbol,
                                                  ' / '.join(labels), *stats]))
                    except Exception as exc:
                        failed += 1
                        reason = str(exc).split('\n')[0][:140]
                        reasons[reason] = reasons.get(reason, 0) + 1
                        if failed <= 5:
                            self.q.put(('log', f'{code} {name}：{reason}'))
                    done_count += 1
                    time.sleep(0.12)
                summary = (f'掃描結束：已處理 {done_count}/{len(pool)} 檔；符合 {matches} 檔；'
                           f'下載失敗／資料不足 {failed} 檔。')
                if self.stop_event.is_set():
                    summary = '已停止。' + summary
                if reasons:
                    top_reason = sorted(reasons.items(), key=lambda x: -x[1])[:3]
                    summary += ' 主要原因：' + '；'.join(f'{k}（{v}檔）' for k, v in top_reason)
                self.q.put(('status', summary))
            except Exception as exc:
                self.q.put(('error', f'全市場掃描中斷：{type(exc).__name__}: {exc}'))
            finally:
                self.q.put(('done',))
        self.start_task(job)

    def selected(self, _event):
        ids = self.tree.selection()
        if not ids: return
        row = self.results[int(ids[0])]
        self.draw_async(row[8], f'{row[0]} {row[1]}')

    def lookup(self):
        code = re.findall(r'\d{4,6}', self.custom.get())
        if not code:
            messagebox.showinfo('請輸入代號', '請在「自選代號」欄輸入，例如 2330。'); return
        c = code[0]
        known = {r[0]: r for r in self.universe}
        row = known.get(c, [c, c, '', c+'.TW'])
        self.draw_async(row[3], f'{row[0]} {row[1]}')

    def draw_async(self, symbol, title):
        def job():
            try:
                d = indicators(fetch(symbol))
                self.q.put(('chart', d, title))
            except Exception as exc:
                self.q.put(('error', f'{title} 圖表取得失敗：{exc}'))
            finally:
                self.q.put(('done',))
        self.status.set(f'下載 {title} 技術線圖…')
        self.start_task(job)

    def draw(self, d, title):
        self.fig.clear()
        ax1 = self.fig.add_subplot(411)
        ax2 = self.fig.add_subplot(412, sharex=ax1)
        ax3 = self.fig.add_subplot(413, sharex=ax1)
        ax4 = self.fig.add_subplot(414, sharex=ax1)
        d = d.tail(110)
        x = list(range(len(d)))
        # Candlesticks: Taiwan convention red up, green down.
        for i, (_, r) in enumerate(d.iterrows()):
            color = '#d64b45' if r['Close'] >= r['Open'] else '#24936c'
            ax1.plot([i, i], [r['Low'], r['High']], color=color, linewidth=0.7)
            ax1.add_patch(Rectangle((i-.32, min(r['Open'],r['Close'])), .64,
                                    max(abs(r['Close']-r['Open']), .01), facecolor=color, edgecolor=color))
        for name in ('MA5', 'MA10', 'MA20', 'MA60'):
            ax1.plot(x, d[name], linewidth=1.1, label=name)
        ax1.set_title(f'{title}  |  日K／均線  |  最新資料：{d.index[-1].date()}', fontsize=11)
        ax1.legend(loc='upper left', fontsize=7, ncol=4)
        ax2.bar(x, d['Volume']/1000, color=['#d64b45' if c>=o else '#24936c' for o,c in zip(d['Open'],d['Close'])], width=.7)
        ax2.plot(x, d['Volume'].rolling(20).mean()/1000, color='#e2a52b', linewidth=1, label='20日均量')
        ax2.set_ylabel('量(千股)', fontsize=8); ax2.legend(loc='upper left', fontsize=7)
        ax3.plot(x, d['K'], label='K', color='#d77e17'); ax3.plot(x, d['D'], label='D', color='#346eb1')
        ax3.axhline(80, ls=':', color='gray'); ax3.axhline(20, ls=':', color='gray')
        ax3.set_ylim(0, 100); ax3.set_ylabel('KD', fontsize=8); ax3.legend(loc='upper left', fontsize=7)
        ax4.plot(x, d['DIF'], label='DIF'); ax4.plot(x, d['DEA'], label='Signal')
        ax4.bar(x, d['MACD'], alpha=.35, label='MACD'); ax4.legend(loc='upper left', fontsize=7)
        ax4.set_ylabel('MACD', fontsize=8)
        ticks = list(range(0,len(d),max(1,len(d)//8)))
        ax4.set_xticks(ticks, [d.index[i].strftime('%m/%d') for i in ticks], fontsize=8)
        self.fig.subplots_adjust(left=.065, right=.98, top=.93, bottom=.07, hspace=.12)
        self.canvas.draw_idle()
        self.status.set(f'{title}：收盤 {d["Close"].iloc[-1]:.2f}；K {d["K"].iloc[-1]:.1f}／D {d["D"].iloc[-1]:.1f}；RSI14 {d["RSI"].iloc[-1]:.1f}（RSI數值顯示於此）')

    def export(self):
        if not self.results:
            messagebox.showinfo('無資料', '目前沒有符合條件的股票。'); return
        path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV','*.csv')], initialfile='選股結果.csv')
        if path:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f); writer.writerow(['代號','名稱','市場','收盤','最新成交量(張)','放量倍數','前N日最高收盤價','行情日期','Yahoo代號','型態分類','歷史跌幅%','量縮%','爆量倍數','距低點%'])
                writer.writerows(self.results)
            messagebox.showinfo('已匯出', path)

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == 'universe':
                    self.universe = item[1]
                    self.status.set(f'股票名冊 {len(self.universe)} 檔；{item[2]}')
                elif kind == 'error':
                    self.status.set(item[1]); messagebox.showerror('執行錯誤', item[1])
                elif kind == 'progress':
                    self.progress['maximum'] = item[2]; self.progress['value'] = item[1]
                    self.status.set(item[3])
                elif kind == 'match':
                    row = item[1]; iid = str(len(self.results))
                    self.results.append(row)
                    self.tree.insert('', 'end', iid=iid, values=row[:8] + row[9:])
                elif kind == 'log': self.status.set('資料異常：' + item[1])
                elif kind == 'chart': self.draw(item[1], item[2])
                elif kind == 'status': self.status.set(item[1])
                elif kind == 'done': self.busy = False
        except queue.Empty:
            pass
        self.root.after(120, self.poll)


if __name__ == '__main__':
    root = tk.Tk()
    try:
        style = ttk.Style()
        if 'vista' in style.theme_names(): style.theme_use('vista')
    except Exception: pass
    App(root)
    root.mainloop()

