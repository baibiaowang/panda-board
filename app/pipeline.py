"""Daily ingestion + current prices, with explicit partial failure accounting."""
from __future__ import annotations
import os
import sys
import time
from datetime import date,timedelta
from . import db,store,window
from .config import get_fetch_config
from .locking import writer_lock,BusyError
from .sources import get_source
from .timeutil import now_cn,now_naive,today_cn
from .taxonomy import get_engine


def lock_available():
    try:
        with writer_lock(): return True
    except BusyError: return False


def _kline_target_end():
    now=now_cn()
    # Request only final daily bars. No pre-close snapshot can masquerade as daily close.
    return (now.date() if now.hour>=16 else now.date()-timedelta(days=1)).isoformat()


def _sync_klines(src,event_codes,end):
    market=getattr(src,'market',None)
    cutoff=(now_naive()-timedelta(hours=4)).isoformat(sep=' ')
    covered={r['code'] for r in db.query("SELECT code FROM kline_sync WHERE target_end=? AND status='success' AND checked_at>=?",(end,cutoff))}
    todo=sorted(set(event_codes)-covered)
    saved=0
    errors=[]
    validation_failures={}
    empty=[]
    for code,rows in src.klines_batch(todo,'',end):
        if rows:
            try: saved+=store.save_kline_snapshot(code,rows,end)
            except Exception as exc: validation_failures[code]=str(exc)
    failures={**getattr(market,'kline_errors',{}),**validation_failures}
    errors.extend(f'{c}: {e}' for c,e in failures.items())
    empty=sorted(getattr(market,'kline_empty',set()))
    for code in todo:
        error=failures.get(code)
        if error or code in empty:
            with db.transaction() as c:
                c.execute('UPDATE kline_sync SET status=?,error=?,checked_at=? WHERE code=?',
                    ('failed' if error else 'empty',error or '该区间源返回空行情',now_naive(),code))
    store.meta_set('kline_last_attempt',__import__('json').dumps({'target_end':end,'requested':len(todo),
        'errors':len(errors),'empty':len(empty),'at':now_cn().isoformat()},ensure_ascii=False))
    return saved,errors,empty


def _sync_market_caps(src,codes,day):
    """按 (code, date) 记录市值。返回一个快照里真正写进去的行数和失败明细。

    腾讯行情支持一次拼 60 个代码，所以这里是批量取，不是逐条问。
    """
    market=getattr(src,'market',None)
    if market is None or not hasattr(market,'market_caps'): return 0,{}
    wanted=sorted({c for c in codes if c})
    if not wanted: return 0,{}
    values=market.market_caps(wanted)
    saved=store.save_market_caps((c,day,v) for c,v in values.items())
    return saved,dict(getattr(market,'market_cap_errors',{}) or {})


def run_pipeline(mode='incremental',start=None,end=None,lookback_days=None,source=None,with_klines=True,planned_dates=None):
    with writer_lock():
        return _run_pipeline(mode,start,end,lookback_days,source,with_klines,planned_dates)


def _run_pipeline(mode,start,end,lookback_days,source,with_klines,planned_dates):
    src=None
    run_id=None
    days=None
    began=time.monotonic()
    try:
        if mode not in ('incremental','full'):
            raise ValueError('mode 必须为 incremental/full')
        cfg=get_fetch_config()
        lookback=int(lookback_days if lookback_days is not None else cfg.get('lookback_days',2))
        if lookback<1: raise ValueError('lookback_days 必须为正数')
        end=end or today_cn().isoformat()
        date.fromisoformat(end)
        if end>today_cn().isoformat(): raise ValueError('公告结束日期不能晚于北京时间今天')
        src=source or get_source()
        engine=get_engine()
        db.init_db()
        if planned_dates is not None:
            days=list(dict.fromkeys(planned_dates))
            if not days or len(days)>3661:
                raise ValueError('重试日期列表无效')
            for day in days:
                window.date_range(day,day)
                if day>end: raise ValueError('重试日期超出结束日期')
        elif start is not None:
            days=window.date_range(start,end)
        elif mode=='incremental':
            days=window.plan_dates(end,lookback,src.name,int(cfg.get('scan_days',90)),int(cfg.get('days_per_run',12)))
        else:
            days=window.date_range((date.fromisoformat(end)-timedelta(days=lookback-1)).isoformat(),end)
        run_id=store.start_run(mode)
        stats={'fetched':0,'new':0,'changed':0,'noise':0,'stocks':0,'klines':0}
        errors=[]
        complete_days=0
        stream=iter(src.fetch_dates(days))
        stream_failed=None
        for day in days:
            try:
                if stream_failed: raise RuntimeError(stream_failed)
                try: result=next(stream)
                except Exception as exc:
                    stream_failed=f'来源流中断: {type(exc).__name__}: {exc}'
                    raise RuntimeError(stream_failed) from exc
                if result.date!=day or result.source!=src.name:
                    raise ValueError('数据源没有返回唯一的对应日期结果')
                counts=store.save_day(result,engine)
                stats['fetched']+=result.fetched
                for key,value in counts.items(): stats[key]+=value
                complete_days+=int(result.complete)
                if not result.complete: errors.append(f'{day}: {result.error or "抓取不完整"}')
                print(f'[ann] {day} raw={result.fetched}/{result.expected} complete={result.complete}',flush=True)
            except Exception as exc:
                errors.append(f'{day}: {type(exc).__name__}: {exc}')
                # Record failure only; no successful/old announcement is removed.
                from .sources.types import DayResult
                store.save_day(DayResult(day,src.name,error=str(exc)),engine)
        # Every stock visible in the recent board needs current prices and market value.
        since=(today_cn()-timedelta(days=int(cfg.get('scan_days',90))-1)).isoformat()
        codes={r['code'] for r in db.query("SELECT DISTINCT code FROM announcements WHERE category<>'other' AND is_noise=0 AND date>=?",(since,))}
        if with_klines:
            stats['klines'],price_errors,empty=_sync_klines(src,codes,_kline_target_end())
            # 少数股票拿不到行情是常态（个别代码所在板块源本身不通）。
            # 不能因为几只失败就判整轮失败 —— 那等于永远不提交、网站永远不更新。
            kline_limit=float(cfg.get('kline',{}).get('max_failure_ratio',0.2))
            tolerated=max(20,int(kline_limit*len(codes)))
            if price_errors and len(price_errors)>tolerated:
                errors.append(f'K线 {len(price_errors)} 只失败（阈值 {tolerated}）: '+ '; '.join(price_errors[:5]))
            stats['kline_errors']=len(price_errors)
            stats['kline_empty']=len(empty)
        # 市值按 (code, date) 记账，与 K 线同一批股票、同样只在窗口内取。
        cap_day=today_cn().isoformat()
        stats['market_caps'],cap_errors=_sync_market_caps(src,codes,cap_day)
        cap_limit=float(cfg.get('market_cap',{}).get('max_failure_ratio',0.2))
        if codes and cap_errors and len(cap_errors)/len(codes)>cap_limit:
            errors.append(f'市值失败 {len(cap_errors)}/{len(codes)} 只，超过阈值 {cap_limit:.0%}')
        stats['market_cap_errors']=len(cap_errors)
        src.close()
        src=None
        elapsed=int((time.monotonic()-began)*1000)
        store.finish_run(run_id,fetched=stats['fetched'],new_count=stats['new'],duration_ms=elapsed,
            peak_rss_mb=_peak_rss_mb(),errors=errors)
        return {'ok':not errors,'status':'partial' if errors else 'success','run_id':run_id,'mode':mode,
            'start':min(days),'end':max(days),'dates':days,'complete_days':complete_days,
            'error':'; '.join(errors)[:2000] if errors else None,'errors':errors,
            'publishable':True,'duration_ms':elapsed,**stats}
    except Exception as exc:
        if run_id is not None: store.fail_run(run_id,str(exc),int((time.monotonic()-began)*1000))
        return {'ok':False,'status':'failed','run_id':run_id,'dates':days,'publishable':False,'error':f'{type(exc).__name__}: {exc}'}
    finally:
        if src is not None:
            try: src.close()
            except Exception as exc: print(f'[close] {type(exc).__name__}',flush=True)


def _peak_rss_mb():
    try:
        import resource
        value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value/(1048576 if sys.platform=='darwin' else 1024))
    except ImportError: return None


def run_with_retry(max_attempts=2,delay_seconds=10,**kw):
    if max_attempts<1 or delay_seconds<0: raise ValueError('重试次数必须为正，等待秒数不能为负')
    last={}
    for attempt in range(1,max_attempts+1):
        last=run_pipeline(**kw)
        last['attempt']=attempt
        if last.get('ok') or kw.get('source') is not None: break
        if attempt<max_attempts:
            # Retry the SAME requested dates; do not silently advance the historical queue.
            if last.get('dates'): kw['planned_dates']=last['dates']
            time.sleep(delay_seconds)
    return last


def run_update(mode='incremental',start=None,end=None,lookback_days=None,max_attempts=2,retry_delay=10):
    try:
        with writer_lock():
            return run_with_retry(max_attempts=max_attempts,delay_seconds=retry_delay,mode=mode,
                start=start,end=end,lookback_days=lookback_days)
    except BusyError as exc:
        return {'ok':False,'conflict':True,'error':str(exc)}
