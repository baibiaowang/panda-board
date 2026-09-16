"""Six digit mainland stock identifiers; preserve leading zeros."""
import re
BJ_PREFIXES = ('83', '87', '88', '43', '92')
A_STOCK_RE = re.compile(r'^(?:60\d{4}|68\d{4}|00\d{4}|30\d{4}|(?:83|87|88|43|92)\d{4})$')


def valid_code(code):
    return isinstance(code, str) and A_STOCK_RE.fullmatch(code) is not None


def board_of(code):
    if not valid_code(code):
        return '其他'
    if code.startswith('68'):
        return '科创板'
    if code.startswith('30'):
        return '创业板'
    if code.startswith(BJ_PREFIXES):
        return '北交所'
    return '主板'


def is_st(name):
    return 'ST' in str(name or '').upper()


def secid(code):
    if not valid_code(code):
        raise ValueError('无效的六位A股代码')
    return ('1.' if code.startswith('6') else '0.') + code


def tx_symbol(code):
    if not valid_code(code):
        raise ValueError('无效的六位A股代码')
    return ('bj' if code.startswith(BJ_PREFIXES) else 'sh' if code.startswith('6') else 'sz') + code
