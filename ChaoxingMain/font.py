"""超星加密字体解码。

部分章节作业的题干和选项用平台下发的自定义字体加密：码位不变，字体里的字形被换成
别的字。字形哈希只由轮廓坐标和 on-curve 标志决定，与字体文件无关，所以把下发的字体
逐字算哈希，再对回常见字体的 Unicode 名称即可还原原文。

算法和映射表来自 chaoxing 项目（api/cxsecret_font.py，参考 SocialSisterYi 的
xuexiaoyi-to-xuexitong-tampermonkey-proxy）。映射表是 1.5 MB 的静态数据，第一次
解码时才读入。
"""
import base64
import hashlib
import json
import re
import threading
from io import BytesIO
from pathlib import Path

from MoocMain.log import Logger

logger = Logger(__name__).get_log()

MAP_PATH = Path(__file__).resolve().parent / "resource" / "font_map_table.json"
FONT_DATA_PREFIX = "data:application/font-ttf;charset=utf-8;base64,"
_BASE64_IN_CSS = re.compile(r"base64,([\w\W]+?)'")
_STYLE_TAG = re.compile(
    r'<style[^>]*\bid=["\']cxSecretStyle["\'][^>]*>(.*?)</style>', re.S)


class FontDecodeError(RuntimeError):
    """页面字体不是 TrueType，或映射表缺失；调用方应把该作业当作暂不支持。"""


KX_RADICALS_TAB = str.maketrans(
    # 康熙部首
    "⼀⼁⼂⼃⼄⼅⼆⼇⼈⼉⼊⼋⼌⼍⼎⼏⼐⼑⼒⼓⼔⼕⼖⼗⼘⼙⼚⼛⼜⼝⼞⼟⼠⼡⼢⼣⼤⼥⼦⼧⼨⼩⼪⼫⼬⼭⼮⼯⼰⼱⼲⼳⼴⼵⼶⼷⼸⼹⼺⼻⼼⼽⼾⼿⽀⽁⽂⽃⽄⽅⽆⽇⽈⽉⽊⽋⽌⽍⽎⽏⽐⽑⽒⽓⽔⽕⽖⽗⽘⽙⽚⽛⽜⽝⽞⽟⽠⽡⽢⽣⽤⽥⽦⽧⽨⽩⽪⽫⽬⽭⽮⽯⽰⽱⽲⽳⽴⽵⽶⽷⽸⽹⽺⽻⽼⽽⽾⽿⾀⾁⾂⾃⾄⾅⾆⾇⾈⾉⾊⾋⾌⾍⾎⾏⾐⾑⾒⾓⾔⾕⾖⾗⾘⾙⾚⾛⾜⾝⾞⾟⾠⾡⾢⾣⾤⾥⾦⾧⾨⾩⾪⾫⾬⾭⾮⾯⾰⾱⾲⾳⾴⾵⾶⾷⾸⾹⾺⾻⾼髙⾽⾾⾿⿀⿁⿂⿃⿄⿅⿆⿇⿈⿉⿊⿋⿌⿍⿎⿏⿐⿑⿒⿓⿔⿕⺠⻬⻩⻢⻜⻅⺟⻓",
    # 对应汉字
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋比毛氏气水火爪父爻爿片牙牛犬玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛辰辵邑酉采里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠民齐黄马飞见母长",
)

_table = None
_table_lock = threading.Lock()


def mapping():
    """hash -> Unicode 名称；读表失败只失败一次，不缓存异常。"""
    global _table
    if _table is None:
        with _table_lock:
            if _table is None:
                try:
                    data = json.loads(MAP_PATH.read_text("utf-8"))
                except (OSError, ValueError) as error:
                    raise FontDecodeError("读取字体映射表失败：%s" % error) from None
                _table = {digest: name for name, digest in data.items()}
    return _table


def glyph_hash(glyph):
    """不同字体文件里同一字形的哈希相同：只取各轮廓的坐标和 on-curve 标志。"""
    if glyph.numberOfContours <= 0:
        return ""
    parts, start = [], 0
    for index in range(glyph.numberOfContours):
        end = glyph.endPtsOfContours[index]
        for point in range(start, end + 1):
            x, y = glyph.coordinates[point]
            parts.append("%s%s%s" % (x, y, glyph.flags[point] & 0x01))
        start = end + 1
    return hashlib.md5("".join(parts).encode()).hexdigest()


def font_map(font):
    """解析下发字体，返回 uniXXXX -> 字形哈希。"""
    from fontTools.ttLib import TTFont

    try:
        with TTFont(font, lazy=False) as handle:
            glyphs = handle["glyf"]
            return {name: glyph_hash(glyphs[name])
                    for name in glyphs.glyphOrder if name.startswith("uni")}
    except Exception as error:
        raise FontDecodeError("解析加密字体失败：%s" % error) from None


def decrypt(glyphs, text):
    """把加密文本还原成常用汉字；映射不到的字原样保留。"""
    table = mapping()
    result = []
    for char in text:
        digest = glyphs.get("uni%X" % ord(char))
        name = table.get(digest) if digest else None
        if name is not None and name.startswith("uni"):
            try:
                result.append(chr(int(name[3:], 16)))
                continue
            except ValueError:
                pass
        result.append(char)
    return "".join(result).translate(KX_RADICALS_TAB)


def encrypted(html_text):
    """题目页是否带加密字体样式；不解码就作答会把乱码当题干发给 AI。"""
    return bool(_STYLE_TAG.search(html_text or ""))


class FontDecoder:
    """一份题目页面里下发的字体，同页所有题干和选项共用一个解码器。"""

    def __init__(self, html_text):
        found = _STYLE_TAG.search(html_text or "")
        data = _BASE64_IN_CSS.search(found.group(1)) if found else None
        if not data:
            raise FontDecodeError("页面里没有加密字体数据")
        try:
            raw = base64.b64decode(data.group(1))
        except (ValueError, TypeError) as error:
            raise FontDecodeError("解码字体数据失败：%s" % error) from None
        self.glyphs = font_map(BytesIO(raw))

    def decode(self, text):
        return decrypt(self.glyphs, text) if text else text
