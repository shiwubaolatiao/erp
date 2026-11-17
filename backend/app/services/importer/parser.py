"""导入引擎 — 表头定位 / 别名映射 / 国家与币种识别 / 日期解析
移植自旧系统 import_data.py 中久经验证的解析逻辑，并按文档 03-报表解析规范.md 落地为可配置别名。
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional


# ================= 日期解析（移植自旧系统 _detect_date_format，实测覆盖法/意/荷/瑞/西/德语月份） =================

MONTHS_MAP = {
    # 英文（作为其它语言缺失时的兜底；各语言的差异词单独登记）
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    # 德语
    'jän': 1, 'jänner': 1, 'mär': 3, 'märz': 3, 'mai': 5, 'okt': 10, 'dez': 12,
    # 法语（实测：'1 juin 2026 00:45:12 UTC'、'9 avr. 2026 15:20:22 UTC'）
    'janv': 1, 'fév': 2, 'avr': 4, 'avril': 4, 'juin': 6, 'juil': 7, 'aoû': 8, 'déc': 12,
    # 意大利语
    'gen': 1, 'mag': 5, 'giu': 6, 'lug': 7, 'ago': 8, 'set': 9, 'ott': 10, 'dic': 12,
    # 荷兰语
    'mrt': 3, 'mei': 5,
    # 瑞典语
    'maj': 5,
    # 波兰语
    'sty': 1, 'lut': 2, 'kwi': 4, 'cze': 6, 'lip': 7, 'sie': 8,
    'wrz': 9, 'paź': 10, 'paz': 10, 'lis': 11, 'gru': 12,
    # 西班牙语
    'ene': 1, 'abr': 4,
}

# 按长度降序匹配（更具体的词优先，避免前缀误判，如 'juin' 不会被 'jul'/'jun' 抢先）
_MONTH_KEYS_SORTED = sorted(MONTHS_MAP.keys(), key=len, reverse=True)


def _strip_timezone(s: str) -> str:
    """剥离尾部时区标记
    实测形态：UTC / PST / MEZ / CET / BST / GMT-7 / GMT+8 / +05:30 / -03:00

    ★ 注意：必须保护 AM/PM —— 否则 '3 Jan 2026 3:08:30 PM' 会把 'PM' 当
      时区剥掉，导致 12 小时制不生效（下午 3 点被算成凌晨 3 点）。
    """
    prev = None
    out = s
    while prev != out:
        prev = out
        m = re.search(
            r'\s*(?:\(?(?:GMT|UTC)\s*[+-]\s*\d{1,2}(?::?\d{2})?\)?|[+-]\d{2}:?\d{2}|[A-Za-z]{2,5})$',
            out)
        if not m:
            break
        token = m.group(0).strip()
        if re.fullmatch(r'(?i)(AM|PM|A|P)', token):   # 保护 AM/PM
            break
        out = out[:m.start()].strip()
    return out


def _normalize_ampm(s: str) -> str:
    """把各种 AM/PM 写法统一成 AM/PM
    实测：AM / PM / a.m. / p.m. / a. m. / p. m. / am / pm
    """
    s2 = re.sub(r'\b([ap])\s*\.?\s*[mM]\s*\.?', lambda m: m.group(1).upper() + 'M', s)
    return s2


def parse_date(value) -> Optional[datetime]:
    """多格式日期解析（全部为实测 Amazon 报表格式）

    覆盖：
      ISO 带时区          2026-06-15T00:02:00+00:00 / 2026-01-21T17:19:59+05:30
      德语点分            01.01.2026 14:03:54 UTC / 01.01.2026 19:06:54 MEZ
      英文日在前          31 May 2026 23:04:30 / 1 Jan 2026 12:48:25 UTC
      美式月在前          Mar 1, 2026 1:52:01 AM PST / Jan 2, 2026 8:53:37 a.m. PST
      西班牙语日在前      29 mar 2026 3:46:58 p.m. GMT-7
      斜杠 / 连字符 / Excel 序列号
    返回 None 表示无法解析（调用方决定是否入异常池）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    # ISO 带时区
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        pass
    s_clean = _strip_timezone(s)
    s_ampm = _normalize_ampm(s_clean)
    # 数字型格式（对原串与去时区串都尝试）
    for cand in (s, s_clean, s_ampm):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
                    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M"):
            try:
                return datetime.strptime(cand, fmt)
            except ValueError:
                pass
    # 文本型格式：AM/PM 归一后匹配，两种语序都试
    for cand in (s_ampm, s_clean):
        # 日在前：31 May 2026 23:04:30 / 3 Jan 2026 5:28:25 AM / 29 mar 2026 3:46:58 PM
        m = re.match(
            r'^(\d{1,2})\s+([A-Za-zÀ-ÖØ-öø-ÿĀ-ž]+)\.?\s+(\d{4})'
            r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?)?$', cand)
        if m:
            mon = _month_from_word(m.group(2).lower()[:4])
            if mon:
                dt = _build_dt(int(m.group(3)), mon, int(m.group(1)),
                               m.group(4), m.group(5), m.group(6), m.group(7))
                if dt:
                    return dt
        # 月在前（美式）：Mar 1, 2026 1:52:01 AM / Jan 2, 2026 8:53:37 a.m.
        m = re.match(
            r'^([A-Za-zÀ-ÖØ-öø-ÿĀ-ž]+)\.?\s+(\d{1,2}),?\s+(\d{4})'
            r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?)?$', cand)
        if m:
            mon = _month_from_word(m.group(1).lower()[:4])
            if mon:
                dt = _build_dt(int(m.group(3)), mon, int(m.group(2)),
                               m.group(4), m.group(5), m.group(6), m.group(7))
                if dt:
                    return dt
    # Excel 序列号
    if re.match(r'^\d{5}(\.\d+)?$', s):
        try:
            from openpyxl.utils.datetime import from_excel
            return from_excel(float(s))
        except Exception:
            return None
    return None


def _build_dt(year: int, mon: int, day: int, hh, mm, ss, ampm) -> Optional[datetime]:
    """构造 datetime，处理 12 小时制 AM/PM"""
    h = int(hh or 0)
    mi = int(mm or 0)
    se = int(ss or 0)
    a = (ampm or '').upper()
    if a == 'PM' and h < 12:
        h += 12
    elif a == 'AM' and h == 12:
        h = 0
    try:
        return datetime(year, mon, day, h, mi, se)
    except ValueError:
        return None


def _month_from_word(word: str) -> Optional[int]:
    """从月份词（各语言）解析月号
    按 key 长度降序匹配，避免前缀误判（juin 不会被 jui/jun 抢先）
    """
    w = (word or '').lower()
    for key in _MONTH_KEYS_SORTED:
        if w.startswith(key):
            return MONTHS_MAP[key]
    return None


# ================= 数值解析（移植自旧系统 _safe_decimal + 欧洲数字格式） =================

_UNICODE_MINUS = '\u2212'  # Unicode 减号


def parse_decimal(value, default=None) -> Optional[Decimal]:
    """安全 Decimal：支持欧洲格式 '1.234,56'、'1,234.56'、括号负数、Unicode 减号、货币符号
    返回 None 表示无法解析（default 提供时返回 default）。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    s = str(value).strip()
    if not s:
        return default
    s = s.replace(_UNICODE_MINUS, '-')
    # 括号负数
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    # 货币符号
    s = re.sub(r'[€£$¥]', '', s).strip()
    # 欧洲格式：1.234,56 → 1234.56
    if re.match(r'^-?\d{1,3}(\.\d{3})+,\d+$', s):
        s = s.replace('.', '').replace(',', '.')
    # 千分位逗号：1,234.56
    elif re.match(r'^-?\d{1,3}(,\d{3})+(\.\d+)?$', s):
        s = s.replace(',', '')
    # 纯逗号小数（无千分位证据）：12,5 → 12.5（仅当只有1个逗号且无点）
    elif ',' in s and '.' not in s and s.count(',') == 1:
        s = s.replace(',', '.')
    try:
        return Decimal(s)
    except InvalidOperation:
        return default


def parse_int(value, default=0) -> int:
    """安全整数解析（先走 parse_decimal，兼容 "1,234" / "1.0" / 空值）

    无法解析时返回 default（不抛异常）—— 报表里数量列经常是空的。
    """
    d = parse_decimal(value, None)
    if d is None:
        return default
    try:
        return int(d)
    except Exception:
        return default


def normalize_period_month(value) -> str:
    """把各种"月份"写法归一成 'YYYY-MM'

    ★ 实测坑：仓储费/退货处理费报表的 month_of_charge 列是**带时间戳的日期**
      （'2026-06-01 00:00:00'）、超龄仓储费的 snapshot_date 是 ISO 带时区
      （'2026-01-15T00:00:00+00:00'）。若直接用 '==' 与 'YYYY-MM' 比较会
      **永远匹配不到**，导致仓储费/退货费在核算里全部算作 0。

    支持：'2026-06' / '2026-06-01' / '2026-06-01 00:00:00' / '2026/06' /
          '2026-06-01T00:00:00+00:00' / 'June 2026' / '2026年6月' / Excel 日期对象
    解析不出返回空串。
    """
    if value is None:
        return ''
    # Excel 日期/时间对象
    if isinstance(value, datetime):
        return value.strftime('%Y-%m')
    s = str(value).strip()
    if not s:
        return ''
    # 已归一
    if re.fullmatch(r'\d{4}-\d{2}', s):
        return s
    # 数字型：2026-06-01 / 2026/06/01 / 20260601
    m = re.match(r'^(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    if re.fullmatch(r'\d{6}', s):
        return f"{s[:4]}-{s[4:6]}"
    # 中文：2026年6月
    m = re.match(r'^(\d{4})\s*年\s*(\d{1,2})\s*月', s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    # 英文月名：June 2026 / Jun 2026 / 2026 June
    for part_year, part_mon in ((s.split()[-1], s.split()[0]) if len(s.split()) >= 2 else (None, None),):
        if part_year and part_mon and re.fullmatch(r'\d{4}', part_year):
            mon = _month_from_word(re.sub(r'[^A-Za-z]', '', part_mon).lower()[:4])
            if mon:
                return f"{int(part_year):04d}-{mon:02d}"
    m = re.match(r'^([A-Za-z]+)\s+(\d{4})$', s)
    if m:
        mon = _month_from_word(m.group(1).lower()[:4])
        if mon:
            return f"{int(m.group(2)):04d}-{mon:02d}"
    # 兜底：取其中的日期再格式化
    d = parse_date(s)
    if d:
        return d.strftime('%Y-%m')
    return ''


# ================= 国家/站点识别（移植自旧系统 _detect_country_from_data 的实测规则） =================

COUNTRY_CODES = ['US', 'UK', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'BE', 'IE', 'CA', 'MX', 'AE', 'SA', 'AU', 'PL', 'JP']

MARKETPLACE_HINTS = {
    'amazon.com': 'US', 'amazon.co.uk': 'UK', 'amazon.uk': 'UK', 'amazon.de': 'DE',
    'amazon.fr': 'FR', 'amazon.es': 'ES', 'amazon.it': 'IT', 'amazon.nl': 'NL',
    'amazon.se': 'SE', 'amazon.com.be': 'BE', 'amazon.be': 'BE', 'amazon.ie': 'IE',
    'amazon.ca': 'CA', 'amazon.com.mx': 'MX', 'amazon.mx': 'MX', 'amazon.ae': 'AE',
    'amazon.sa': 'SA', 'amazon.com.au': 'AU', 'amazon.pl': 'PL', 'amazon.co.jp': 'JP',
}

CURRENCY_TO_COUNTRY = {
    'USD': 'US', 'GBP': 'UK', 'EUR': 'DE', 'SEK': 'SE', 'CAD': 'CA',
    'MXN': 'MX', 'AED': 'AE', 'SAR': 'SA', 'AUD': 'AU', 'PLN': 'PL', 'JPY': 'JP',
}

# 国家代码（含 ISO-2 变体）→ 本站站点代码；GB→UK、US→US 等
COUNTRY_CODE_ALIAS = {c: c for c in COUNTRY_CODES}
COUNTRY_CODE_ALIAS.update({
    'GB': 'UK', 'UK': 'UK', 'USA': 'US', 'GERMANY': 'DE', 'FRANCE': 'FR',
    'SPAIN': 'ES', 'ITALY': 'IT', 'NETHERLANDS': 'NL', 'SWEDEN': 'SE',
    'BELGIUM': 'BE', 'IRELAND': 'IE', 'CANADA': 'CA', 'MEXICO': 'MX',
    'POLAND': 'PL', 'JAPAN': 'JP',
})

# 中文/英文国家关键词（实测 sheet 名："美国站销售明细"、"墨西哥站销售明细"）
SHEET_COUNTRY_KEYWORDS = {
    '英国': 'UK', 'UK': 'UK', 'GB': 'UK',
    '德国': 'DE', 'DE': 'DE',
    '法国': 'FR', 'FR': 'FR', 'FA': 'FR',   # FA = France 缩写（旧系统实测确认）
    '西班牙': 'ES', 'ES': 'ES',
    '意大利': 'IT', 'IT': 'IT',
    '荷兰': 'NL', 'NL': 'NL',
    '瑞典': 'SE', 'SE': 'SE',
    '比利时': 'BE', 'BE': 'BE',
    '爱尔兰': 'IE', 'IE': 'IE',
    '阿联酋': 'AE', 'AE': 'AE',
    '沙特': 'SA', 'SA': 'SA',
    '澳大利亚': 'AU', '澳洲': 'AU', 'AU': 'AU',
    '美国': 'US', 'US': 'US', 'USA': 'US',
    '加拿大': 'CA', 'CA': 'CA',
    '墨西哥': 'MX', 'MX': 'MX',
    '波兰': 'PL', 'PL': 'PL',
    '日本': 'JP', 'JP': 'JP',
}


def detect_country_from_sheet_name(sheet_name: str) -> Optional[str]:
    """从 sheet 名解析国家
    实测覆盖：UK销售 → UK；CA 4-5销售明细 → CA；美国站销售明细 → US；墨西哥站销售明细 → MX；FA销售 → FR
    """
    sn = (sheet_name or '').strip()
    sn_upper = sn.upper()
    # 1) 中文/英文国家关键词（优先长词匹配，避免 "美国" 被 "美" 类误伤）
    for kw in sorted(SHEET_COUNTRY_KEYWORDS.keys(), key=len, reverse=True):
        if kw in sn or (len(kw) <= 3 and re.search(rf'(?<![A-Z]){re.escape(kw)}(?![A-Z])', sn_upper)):
            return SHEET_COUNTRY_KEYWORDS[kw]
    # 2) 前缀形式：UK销售 / DE销售明细
    m = re.match(r'^\s*([A-Za-z]{2,3})[\s\-–_]', sn)
    if m:
        code = m.group(1).upper()
        if code in COUNTRY_CODE_ALIAS:
            return COUNTRY_CODE_ALIAS[code]
    return None


def detect_country_from_code_column(header: list, row: list) -> Optional[str]:
    """从数据行的 country_code / 国家 列读取国家（多国家 sheet：仓储费/退货费/超龄/移除/入库）"""
    for i, h in enumerate(header):
        if i >= len(row):
            break
        hl = str(h).strip().lower()
        if hl in ('country_code', 'country code', 'country', '国家', '国家代码', '站点'):
            v = str(row[i] or '').strip().upper()
            if v in COUNTRY_CODE_ALIAS:
                return COUNTRY_CODE_ALIAS[v]
            # 中文国家名
            if str(row[i] or '').strip() in SHEET_COUNTRY_KEYWORDS:
                return SHEET_COUNTRY_KEYWORDS[str(row[i]).strip()]
    return None


def detect_country_from_row(header: list, row: list) -> Optional[str]:
    """从数据行推断国家：country_code 列 / marketplace 列 / 币种列"""
    code = detect_country_from_code_column(header, row)
    if code:
        return code
    for i, h in enumerate(header):
        if i >= len(row):
            break
        h_clean = str(h).strip().lower()
        v = str(row[i]).strip().lower()
        if 'marketplace' in h_clean or '网站' in h_clean or '店铺站点' in h_clean:
            for key, code2 in MARKETPLACE_HINTS.items():
                if key in v:
                    return code2
        if h_clean in ('currency', '币种', '货币'):
            c = CURRENCY_TO_COUNTRY.get(v.upper())
            if c:
                return c
    return None


def detect_currency_from_header(header: list) -> Optional[str]:
    """从表头括号解析币种：销售额(GBP) → GBP"""
    for h in header:
        m = re.search(r'\((USD|GBP|EUR|CAD|MXN|SEK|AED|SAR|AUD|PLN|JPY)\)', str(h), re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


# ================= 表头定位（移植旧系统动态表头识别） =================

# ★ 多语言表头归一（移植自旧系统 import_data.py 的 _header_normalize，实测覆盖
#   德/法/意/西/荷/瑞典/波兰 七种语言的 Amazon 报表表头）
# 这是旧系统最有价值的资产：德语等站点表头不归一 → 整站利润算不出来
HEADER_NORMALIZE = {
    # 日期时间
    'datum/uhrzeit': 'date/time', 'date/heure': 'date/time', 'data/ora': 'date/time',
    'data/ora:': 'date/time', 'datum/tijd': 'date/time', 'datum/tid': 'date/time',
    'fecha/hora': 'date/time', 'fecha y hora': 'date/time', 'data/godzina': 'date/time',
    # 结算编号
    'abrechnungsnummer': 'settlement id', 'numéro de versement': 'settlement id',
    'identifiant du paiement': 'settlement id', 'identyfikator rozliczenia': 'settlement id',
    'numero pagamento': 'settlement id', 'schikkings-id': 'settlement id',
    'reglerings-id': 'settlement id', 'id. de liquidación': 'settlement id',
    'identificador de pago': 'settlement id',
    # 类型
    'typ': 'type', 'tipo': 'type',
    # 订单号
    'bestellnummer': 'order id', 'numéro de la commande': 'order id', 'numero ordine': 'order id',
    'bestelnummer': 'order id', 'beställnings-id': 'order id', 'id. del pedido': 'order id',
    'número de pedido': 'order id', 'identyfikator zamówienia': 'order id',
    # 描述
    'beschreibung': 'description', 'descripción': 'description', 'descrizione': 'description',
    'beschrijving': 'description', 'beskrivning': 'description', 'opis': 'description',
    # 数量
    'menge': 'quantity', 'cantidad': 'quantity', 'quantité': 'quantity', 'quantità': 'quantity',
    'aantal': 'quantity', 'antal': 'quantity', 'ilość': 'quantity',
    # 配送方式
    'versand': 'fulfillment', 'cumplimiento': 'fulfillment', 'traitement': 'fulfillment',
    'expédition': 'fulfillment', 'gestión logística': 'fulfillment',
    'gestione': 'fulfillment', 'leverans': 'fulfillment', 'realizacja': 'fulfillment',
    # 站点
    'marknadsplats': 'marketplace', 'web de amazon': 'marketplace', 'site de vente': 'marketplace',
    'rynek': 'marketplace',
    # 城市/州省/邮编
    'ort der bestellung': 'order city', "ville d'où provient la commande": 'order city',
    "città di provenienza dell'ordine": 'order city', 'bestelling stad': 'order city',
    'stad för beställning': 'order city', 'ciudad del pedido': 'order city',
    'ville de la commande': 'order city', 'ciudad de procedencia del pedido': 'order city',
    'miejscowość zamówienia': 'order city',
    'bundesland': 'order state', "région d'où provient la commande": 'order state',
    'état de la commande': 'order state', 'comunidad autónoma de procedencia del pedido': 'order state',
    "provincia di provenienza dell'ordine": 'order state', 'status bestelling': 'order state',
    'delstat för beställning': 'order state', 'estado del pedido': 'order state',
    'stan zamówienia': 'order state',
    'postleitzahl': 'order postal', 'code postal de la commande': 'order postal',
    'commande postale': 'order postal', 'código postal de procedencia del pedido': 'order postal',
    "cap dell'ordine": 'order postal', 'bestelling per post': 'order postal',
    'postadress för beställning': 'order postal', 'código postal del pedido': 'order postal',
    'przekaz pocztowy': 'order postal',
    # 税模型
    'formulario de recaudación de impuestos': 'tax collection model',
    # 销售额
    'umsätze': 'product sales', 'ventes de produits': 'product sales',
    'ventas de productos': 'product sales', 'vendite': 'product sales',
    'verkoop van producten': 'product sales', 'försäljning av produkter': 'product sales',
    'sprzedaż produktów': 'product sales',
    # 销售税
    'produktumsatzsteuer': 'product sales tax', 'impuesto de ventas de productos': 'product sales tax',
    'taxes sur la vente des produits': 'product sales tax',
    'imposta sulle vendite dei prodotti': 'product sales tax',
    'pobrany podatek od sprzedaży': 'product sales tax',
    # 运费收入
    'gutschrift für versandkosten': 'postage credits', "crédits d'expédition": 'postage credits',
    "crédits d'expédition ": 'postage credits', 'abonos de envío': 'postage credits',
    'accrediti per le spedizioni': 'postage credits', 'verzendtegoeden': 'postage credits',
    'fraktkrediter': 'postage credits', 'noty kredytowe za wysyłkę': 'postage credits',
    # 运费税
    'steuer auf versandgutschrift': 'shipping credits tax',
    'impuesto de abono de envío': 'shipping credits tax',
    'impuestos por abonos de envío': 'shipping credits tax',
    'imposta accrediti per le spedizioni': 'shipping credits tax',
    # 礼品包装
    'gutschrift für geschenkverpackung': 'gift wrap credits',
    "crédits sur l'emballage cadeau": 'gift wrap credits',
    "crédits d'emballage-cadeau": 'gift wrap credits',
    'créditos por envoltorio de regalo': 'gift wrap credits',
    'abonos de envoltorio para regalo': 'gift wrap credits',
    'accrediti per confezioni regalo': 'gift wrap credits',
    'kredietpunten cadeauverpakking': 'gift wrap credits',
    'krediter för presentinslagning': 'gift wrap credits',
    'środki na pokrycie pakowania na prezent': 'gift wrap credits',
    # 礼品包装税
    'steuer auf geschenkverpackungsgutschriften': 'giftwrap credits tax',
    'taxes sur les crédits cadeaux': 'giftwrap credits tax',
    'imposta sui crediti confezione regalo': 'giftwrap credits tax',
    'impuesto de créditos de envoltura': 'giftwrap credits tax',
    'impuestos por abonos de envoltorio para regalo': 'giftwrap credits tax',
    # 促销返点
    'rabatte aus werbeaktionen': 'promotional rebates', 'rabais promotionnels': 'promotional rebates',
    'total des réductions': 'promotional rebates', 'descuentos promocionales': 'promotional rebates',
    'devoluciones promocionales': 'promotional rebates', 'sconti promozionali': 'promotional rebates',
    'promotiekortingen': 'promotional rebates', 'kampanjrabatter': 'promotional rebates',
    'rabaty promocyjne': 'promotional rebates',
    # 返点税
    'steuer auf aktionsrabatte': 'promotional rebates tax',
    'taxes sur les remises promotionnelles': 'promotional rebates tax',
    'impuesto de reembolsos promocionales': 'promotional rebates tax',
    'impuestos de descuentos por promociones': 'promotional rebates tax',
    'imposta sugli sconti promozionali': 'promotional rebates tax',
    # 平台代扣税
    'einbehaltene steuer auf marketplace': 'marketplace withheld tax',
    'taxes retenues sur le site de vente': 'marketplace withheld tax',
    'impuesto de retenciones en la plataforma': 'marketplace withheld tax',
    'impuesto retenido en el sitio web': 'marketplace withheld tax',
    'trattenuta iva del marketplace': 'marketplace withheld tax',
    'podatek od transakcji marketplace facilitator': 'marketplace withheld tax',
    # 佣金
    'verkaufsgebühren': 'selling fees', 'frais de vente': 'selling fees',
    'tarifas de venta': 'selling fees', 'commissioni di vendita': 'selling fees',
    'verkoopkosten': 'selling fees', 'försäljningsavgifter': 'selling fees',
    'opłaty za sprzedaż': 'selling fees',
    # FBA 费
    'gebühren zu versand durch amazon': 'fba fees', 'frais expédié par amazon': 'fba fees',
    'frais pour le service expédié par amazon': 'fba fees', 'tarifas fba': 'fba fees',
    'tarifas de logística de amazon': 'fba fees',
    'costi del servizio logistica di amazon': 'fba fees',
    'fba-vergoedingen': 'fba fees', 'fba-avgifter': 'fba fees',
    'fulfilment by amazon fees': 'fba fees', 'fulfillment by amazon fees': 'fba fees',
    'opłaty za fba': 'fba fees',
    # 其他交易费
    'andere transaktionsgebühren': 'other transaction fees',
    'autres frais de transaction': 'other transaction fees',
    'tarifas de otra transacción': 'other transaction fees',
    'tarifas de otras transacciones': 'other transaction fees',
    'altri costi relativi alle transazioni': 'other transaction fees',
    'overige transactiekosten': 'other transaction fees',
    'övriga transaktionsavgifter': 'other transaction fees',
    'inne opłaty transakcyjne': 'other transaction fees',
    # other / total
    'andere': 'other', 'autre': 'other', 'autres': 'other', 'otro': 'other', 'altro': 'other',
    'overige': 'other', 'övrigt': 'other', 'inne': 'other',
    'gesamt': 'total', 'totaal': 'total', 'totalt': 'total', 'suma': 'total', '总计': 'total',
    # 状态 / 释放日期
    'transaktionsstatus': 'transaction status', 'statut de la transaction': 'transaction status',
    'estado de la transacción': 'transaction status', 'stato della transazione': 'transaction status',
    'transactiestatus': 'transaction status', 'status transakcji': 'transaction status',
    'transaktionens utgivningsdatum': 'transaction release date',
    'freigabedatum der transaktion': 'transaction release date',
    'date de sortie de la transaction': 'transaction release date',
    'date de délivrance de la transaction': 'transaction release date',
    'fecha de liberación de la transacción': 'transaction release date',
    'data di rilascio della transazione': 'transaction release date',
    'publicatiedatum van transactie': 'transaction release date',
    'data zrealizowania transakcji': 'transaction release date',
    # ===== 实测补充：IE/FR/BE/NL/SE 站点的税列与其它别名（不补全会导致这些站点 net_amazon 算偏）=====
    'sales tax collected': 'product sales tax',              # IE 站
    'taxe de ventes prélevée': 'product sales tax',          # BE 站
    'geïnde omzetbelasting': 'product sales tax',            # NL 站
    'inkasserad moms': 'product sales tax',                  # SE 站
    'marketplace facilitator tax': 'marketplace withheld tax',      # IE 站
    'taxe marketplace facilitator': 'marketplace withheld tax',     # BE 站
    'belasting voor marketplace-facilitator': 'marketplace withheld tax',  # NL 站
    'skatt för marknadsplatsförmedlare': 'marketplace withheld tax',       # SE 站
    # 注：法语/荷兰语/瑞典语的撇号有 U+0027 与 U+2019 两种写法，全部覆盖
    'crédits d’expédition': 'postage credits',
    'crédits d’expédition ': 'postage credits',
    'taxe sur les crédits d’expédition': 'shipping credits tax',
    "taxe sur les crédits d'expédition": 'shipping credits tax',
    'crédits d’emballage-cadeau': 'gift wrap credits',
    'taxes sur les crédits cadeaux': 'giftwrap credits tax',
    'taxe sur les credits cadeaux': 'giftwrap credits tax',
    'shipping credits': 'postage credits',                   # IE 站用 shipping credits
    'giftwrap credits': 'gift wrap credits',                 # IE 站
    # 广告表：合并「商品」列的第二列（SKU）在归一后仍是空表头，由管线按位置处理
    '展示次数': 'ad impressions', '点击量': 'ad clicks', '订单数量': 'ad orders',
    '花费(gbp)': 'ad spend', '花费(usd)': 'ad spend', '花费(eur)': 'ad spend',
    '支出(gbp)': 'ad spend', '支出(usd)': 'ad spend', '支出(eur)': 'ad spend',
    '销售额(gbp)': 'ad sales', '销售额(usd)': 'ad sales', '销售额(eur)': 'ad sales',
    'ntb 订单数量': 'ad ntb orders', 'ntb 销售额(gbp)': 'ad ntb sales',
    'ntb 销售额(usd)': 'ad ntb sales', 'ntb 销售额(eur)': 'ad ntb sales',
    '可见展示量': 'ad visible impressions', '品牌新客销售额比例': 'ad ntb sales pct',
    '商品推广使用资格': 'ad eligibility', '类型': 'ad type',
    'amazon sku': 'sku', '产品': 'product name', '型号': 'model',
    '欧洲运费': 'freight eu', '英国站运费': 'freight uk', '爱尔兰站运费': 'freight ie',
}

# ★ 多语言交易类型归一（移植自旧系统 _type_map）— 利润算法只认英文 type，必须归一
TRANSACTION_TYPE_MAP = {
    'order': 'Order', 'pedido': 'Order', 'bestellung': 'Order', 'commande': 'Order',
    'ordine': 'Order', 'bestelling': 'Order', 'beställning': 'Order', 'zamówienie': 'Order',
    'refund': 'Refund', 'reembolso': 'Refund', 'erstattung': 'Refund', 'remboursement': 'Refund',
    'rimborso': 'Refund', 'terugbetaling': 'Refund', 'återbetalning': 'Refund', 'zwrot': 'Refund',
    'adjustment': 'Adjustment', 'ajuste': 'Adjustment', 'anpassung': 'Adjustment',
    'ajustement': 'Adjustment', 'aggiustamento': 'Adjustment', 'aanpassing': 'Adjustment',
    'justering': 'Adjustment', 'korekta': 'Adjustment',
    'transfer': 'Transfer', 'überweisung': 'Transfer', 'virement': 'Transfer',
    'bonifico': 'Transfer', 'overboeking': 'Transfer', 'överföring': 'Transfer',
    'przelew': 'Transfer', 'transacción': 'Transfer',
    'service fee': 'Service Fee', 'servicegebühr': 'Service Fee',
    'frais de service': 'Service Fee', 'tarifa de servicio': 'Service Fee',
    'commissione di servizio': 'Service Fee', 'servicekosten': 'Service Fee',
    'amazon fees': 'Amazon Fees', 'amazon-gebühren': 'Amazon Fees',
    'frais amazon': 'Amazon Fees', 'tarifas de amazon': 'Amazon Fees',
    'fba inventory fee': 'FBA Inventory Fee', 'fba-lagerbestandsgebühr': 'FBA Inventory Fee',
    'frais de stock fba': 'FBA Inventory Fee',
    'fulfilment by amazon (fba) transaction fees': 'Fulfilment by Amazon (FBA) transaction fees',
    'fulfillment by amazon (fba) transaction fees': 'Fulfilment by Amazon (FBA) transaction fees',
}


def normalize_header(header: list) -> list:
    """表头多语言 + 分隔符归一

    1) 多语言：Datum/Uhrzeit → date/time
    2) 分隔符折叠：Amazon 报表实际用连字符/下划线（snapshot-date / month_of_charge）
       → 统一折叠为空格，使 "month of charge" 这类标准字段能命中

    ★ 仅用于列映射；ODS 原始层始终保存未归一表头，保证溯源真实。
    """
    out = []
    for h in header:
        s = str(h).strip() if h is not None else ''
        if not s:
            out.append('')
            continue
        mapped = HEADER_NORMALIZE.get(s.lower())
        if mapped:
            out.append(mapped)
            continue
        # 分隔符折叠（连字符/下划线 → 空格），多空格压成单空格
        folded = re.sub(r'[\-_]+', ' ', s)
        folded = re.sub(r'\s+', ' ', folded).strip()
        out.append(folded)
    return out


def normalize_transaction_type(value: str) -> str:
    """交易类型多语言归一：Anpassung → Adjustment（利润算法只认英文 type）"""
    s = (value or '').strip()
    if not s:
        return s
    return TRANSACTION_TYPE_MAP.get(s.lower(), s)


# 已知标准字段的别名词典（初始内置；运行时由 header_alias 表扩展）
BUILTIN_HEADER_ALIASES = {
    'date_time': ['date/time', '日期/时间', '日期 时间', 'date time', '时间', '交易时间', '日期'],
    'settlement_id': ['settlement id', '结算编号', '结算id', 'settlement'],
    'transaction_type': ['type', '类型', '交易类型'],
    'order_id': ['order id', '订单编号', '订单id', '订单号', 'order'],
    'sku': ['sku', 'msku', '卖家sku'],
    'asin': ['asin', 'asin1', 'asin1(marketplace)'],
    'description': ['description', '描述', '产品描述', '说明'],
    'quantity': ['quantity', '数量', 'menge', 'aantal', 'quantità', 'ilość', 'quantité', 'cantidad', 'antal'],
    'marketplace': ['marketplace', '网站', '店铺站点', 'marketplace.1'],
    'fulfillment': ['fulfillment', '配送方式', 'fulfilment'],
    'product_sales': ['product sales', '商品销售额', 'product sales(de)', '商品销售'],
    'product_sales_tax': ['product sales tax', '商品销售税', '销售税'],
    'postage_credits': ['postage credits', '运费收入', 'shipping credits'],
    'shipping_credits_tax': ['shipping credits tax', '运费税'],
    'gift_wrap_credits': ['gift wrap credits', '礼品包装费'],
    'giftwrap_credits_tax': ['giftwrap credits tax', '礼品包装税'],
    'regulatory_fee': ['regulatory fee', '监管费'],
    'tax_on_regulatory_fee': ['tax on regulatory fee', '监管费税'],
    'promotional_rebates': ['promotional rebates', '促销返点', '促销回扣', 'promo rebates'],
    'promotional_rebates_tax': ['promotional rebates tax', '促销返点税'],
    'marketplace_withheld_tax': ['marketplace withheld tax', '平台代扣税'],
    'selling_fees': ['selling fees', '销售佣金', 'selling fee', 'commission'],
    'fba_fees': ['fba fees', 'fba费', 'fba fee', 'fba配送费'],
    'other_transaction_fees': ['other transaction fees', '其他交易费'],
    'other': ['other', '其他', '其他金额'],
    'total': ['total', '总计', '合计', '总额'],
    'transaction_status': ['transaction status', '交易状态'],
    'transaction_release_date': ['transaction release date', '结算释放日期'],
    # 广告
    'ad_product': ['商品', 'product', '商品(entity)'],
    'ad_sales': ['销售额', '销售额(usd)', '销售额(gbp)', '销售额(eur)', 'sales', '销售(gbp)', 'spend' + '_sales_placeholder'],
    'ad_spend': ['花费', '花费(usd)', '花费(gbp)', '花费(eur)', '支出', '支出(usd)', '支出(gbp)', '支出(eur)', 'spend', 'cost', '花费(cad)', '花费(mxn)', '花费(sek)', '支出(cad)', '支出(eur)'],
    'ad_impressions': ['曝光量', '展示量', 'impressions', '曝光数', '曝光量(usd)'],
    'ad_clicks': ['点击量', '点击次数', 'clicks', '点击数'],
    'ad_ctr': ['点击率', 'ctr'],
    'ad_cpc': ['cpc', '每次点击成本', 'cpc(usd)'],
    'ad_orders': ['订单数', '订单量', 'orders', '订单数量(usd)'],
    'ad_roas': ['roas', '广告销售成本比', 'acos'].__class__ is not None and ['roas'] or [],
    # 产品表
    'cost_rmb': ['成本rmb', '成本(rmb)', '成本', '采购成本', '成本价'],
    'freight_per_unit': ['产品运费/台', '运费', '产品运费', '单位运费', '头程运费', '英国站运费', '爱尔兰站运费'],
    # 汇率
    'rate_country': ['国家', 'country'],
    'rate_value': ['汇率', 'rate', 'exchange rate'],
}


def find_col(header: list, *names: str) -> int:
    """精确/包含匹配列名（大小写不敏感、去空白）— 移植旧 _find_col"""
    header_norm = [str(h).strip().lower() for h in header]
    for name in names:
        if name in header_norm:
            return header_norm.index(name)
    return -1


def find_col_like(header: list, *keywords: str) -> int:
    """模糊匹配（包含关键词即命中）— 移植旧 _find_col_like"""
    header_norm = [str(h).strip().lower() for h in header]
    for kw in keywords:
        for i, h in enumerate(header_norm):
            if kw in h:
                return i
    return -1


def map_header_to_canonical(header: list, db_aliases: dict | None = None) -> dict:
    """把表头映射为标准字段 → 列索引 dict

    处理顺序：多语言归一（Datum/Uhrzeit→date/time）→ DB别名 → 内置词典
    db_aliases: 从 header_alias 表加载的 {alias_lower: canonical_field}，优先于内置词典
    返回 {canonical_field: col_index}
    """
    result = {}
    normed = normalize_header(header)                    # 多语言归一
    header_norm = [str(h).strip().lower() for h in normed]
    db_aliases = db_aliases or {}
    # 内置词典反查表（alias_lower → canonical），避免每列重复构造
    builtin_lookup = {}
    for canonical, aliases in BUILTIN_HEADER_ALIASES.items():
        for a in aliases:
            builtin_lookup.setdefault(a.lower(), canonical)
    for i, h in enumerate(header_norm):
        # DB 别名优先
        if h in db_aliases:
            canonical = db_aliases[h]
            if canonical not in result:
                result[canonical] = i
            continue
        if h in builtin_lookup:
            canonical = builtin_lookup[h]
            if canonical not in result:
                result[canonical] = i
    return result


def detect_header_row(rows: list, max_scan: int = 15, db_aliases: dict | None = None) -> int:
    """动态表头定位：扫描前 N 行，找出命中标准字段别名最多的行
    返回表头行号（0-based）；-1 = 无法识别"""
    best_row, best_hits = -1, 0
    for r in range(min(max_scan, len(rows))):
        row = rows[r]
        if not row or all(str(c).strip() == '' for c in row):
            continue
        hits = 0
        mapped = map_header_to_canonical(row, db_aliases)
        hits = len(mapped)
        # 汇率类 sheet 小表头也认（≥2 即候选）
        if hits > best_hits:
            best_row, best_hits = r, hits
    if best_hits >= 3:
        return best_row
    return best_row if best_hits >= 2 else -1


# ================= sheet 类型识别 =================

def detect_report_type(sheet_name: str, db_sheet_aliases: list | None = None) -> tuple[Optional[str], Optional[str]]:
    """识别 sheet 标准类型 + 国家提示
    db_sheet_aliases: [(report_type, alias_name, country_hint), ...]
    返回 (report_type, country_hint)；未识别返回 (None, None)
    """
    name = sheet_name.strip().lower()
    # DB 别名优先
    for report_type, alias, hint in (db_sheet_aliases or []):
        if name == str(alias).strip().lower():
            return report_type, hint
    # 内置规则（依据实测 sheet 名，顺序重要：超龄仓储费必须先于仓储费判断）
    if '超龄' in name or 'aged' in name or 'long term storage' in name or '长期仓储' in name:
        return 'aged_storage', detect_country_from_sheet_name(sheet_name)
    if '汇率' in name or 'exchange' in name or name == 'rate':
        return 'exchange_rate', None
    if '入库' in name or 'inbound' in name:
        return 'inbound', detect_country_from_sheet_name(sheet_name)
    if '移除' in name or 'removal' in name:
        return 'removal', detect_country_from_sheet_name(sheet_name)
    if '退货' in name or '退回' in name or 'return' in name:
        return 'returns', detect_country_from_sheet_name(sheet_name)
    if '仓储费' in name or 'storage fee' in name or 'storage' in name:
        return 'storage', detect_country_from_sheet_name(sheet_name)
    if '广告' in name or name == 'ad' or 'advertising' in name:
        return 'ad', detect_country_from_sheet_name(sheet_name)
    if '产品' in name or 'product' in name:
        return 'product', None
    if '销售' in name or 'sales' in name or '明细' in name or '销' in name:
        # 命中分支说明：
        #   "美国站销售明细" / "US 销售明细" → 中文+英文命名
        #   "SA销"（实测 MGK 存在这种截断命名） → 单个"销"字也视为销售表
        return 'sales', detect_country_from_sheet_name(sheet_name)
    return None, None


# ================= 文件名解析（店铺/大区） =================

STORE_CODES = ['LMG', 'MGK', 'MGT', 'STX', 'WEL']


def parse_store_from_filename(filename: str) -> tuple[Optional[str], Optional[str]]:
    """文件名 → (店铺代码, 大区)。容忍噪声：MGK-EU_01.xlsx / MGT-EU 06.xlsx / WEL-NA01-03.xlsx"""
    base = filename.rsplit('.', 1)[0].upper()
    store = None
    for code in STORE_CODES:
        if re.search(rf'(?<![A-Z]){code}(?![A-Z])', base):
            store = code
            break
    region = None
    if re.search(r'(?<![A-Z])NA(?![A-Z])', base):
        region = 'NA'
    elif re.search(r'(?<![A-Z])EU(?![A-Z])', base):
        region = 'EU'
    return store, region
