import os
from urllib.parse import urlencode, quote
import paper_bot

DATA_BASE = "https://data.alpaca.markets/v1beta3"

def _data(path): return paper_bot.signed_request("GET", DATA_BASE, path, require_paper=False)
def normalize(symbol):
    text=str(symbol or '').upper().replace('-','/').replace(' ','')
    if '/' in text: return text
    for quote_ccy in ('USDT','USDC','USD','BTC'):
        if text.endswith(quote_ccy) and len(text)>len(quote_ccy): return text[:-len(quote_ccy)]+'/'+quote_ccy
    return text

def historical_bars(symbol,timeframe='1Hour',start=None,end=None,limit=10000):
    params={'symbols':normalize(symbol),'timeframe':timeframe,'limit':limit,'sort':'asc'}
    if start: params['start']=start
    if end: params['end']=end
    bars=[]; token=None
    while True:
        q=dict(params)
        if token: q['page_token']=token
        data=_data('/crypto/us/bars?'+urlencode(q))
        block=data.get('bars',{})
        bars.extend(block.get(normalize(symbol),[]) if isinstance(block,dict) else [])
        token=data.get('next_page_token')
        if not token: break
    return bars

def recent_bars(symbol,timeframe='1Hour',days=30):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc); start=end-timedelta(days=days)
    fmt=lambda x:x.isoformat().replace('+00:00','Z')
    return historical_bars(symbol,timeframe,fmt(start),fmt(end))

def tradable_assets():
    assets=paper_bot.alpaca_request('GET','/assets?asset_class=crypto&status=active')
    return {normalize(a.get('symbol')):a for a in assets if a.get('tradable')}

def crypto_positions():
    out=[]
    for p in paper_bot.get_positions():
        sym=normalize(p.get('symbol'))
        if '/' in sym and sym.split('/')[-1] in {'USD','USDT','USDC','BTC'}:
            q=dict(p); q['symbol']=sym; out.append(q)
    return out

def crypto_orders(status='open',limit=100):
    return [o for o in paper_bot.get_orders(status,limit) if '/' in normalize(o.get('symbol'))]

def submit_buy(symbol,dollars):
    return paper_bot.alpaca_request('POST','/orders',{'symbol':normalize(symbol),'notional':str(round(float(dollars),2)),'side':'buy','type':'market','time_in_force':'gtc'})

def submit_sell(symbol,qty):
    return paper_bot.alpaca_request('POST','/orders',{'symbol':normalize(symbol),'qty':str(qty),'side':'sell','type':'market','time_in_force':'gtc'})
