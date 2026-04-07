#!/usr/bin/env python3
"""
PolyWatch API — HTTP wrapper for execute_trade and withdraw logic.
Deployed to Railway as a separate service so n8n (also on Railway) can call it.

Endpoints:
  GET  /           → liveness (for Railway root healthcheck)
  GET  /healthz    → liveness (JSON)
  GET  /proxy/<endpoint>  → CORS-enabled proxy for Polymarket APIs
                            (leaderboard, activity, trades, positions, markets)
  POST /execute-trade  body: JSON signal (approvedUsdc, token_id, price, side, outcome, market)
  POST /withdraw       body: {"amount": float, "to_address": "0x..."}

Env vars:
  POLY_KEY    — Polymarket proxy wallet private key (required for trade/withdraw)
  API_TOKEN   — shared secret, sent by n8n in X-API-Token header (optional)
  POLYGON_RPC — Polygon RPC URL (defaults to https://polygon-rpc.com)
  PORT        — set by Railway; gunicorn binds to this
"""
import os
import logging
from functools import wraps

import requests
from flask import Flask, request, jsonify, Response

# Heavy deps (py_clob_client, web3) are imported lazily inside functions so
# gunicorn workers boot instantly and the healthcheck responds immediately.

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('polywatch-api')

POLY_KEY = os.getenv('POLY_KEY', '')
API_TOKEN = os.getenv('API_TOKEN', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
POLYGON_RPC = os.getenv('POLYGON_RPC', 'https://polygon-rpc.com')
USDC_ADDRESS_STR = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
USDC_ABI = '[{"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}]'

if not POLY_KEY:
    log.warning('POLY_KEY not set — /execute-trade and /withdraw will fail until it is set')

app = Flask(__name__)
log.info('polywatch-api initialized (POLY_KEY set: %s, API_TOKEN set: %s)', bool(POLY_KEY), bool(API_TOKEN))


# ---------- CORS ----------
@app.after_request
def add_cors_headers(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Token'
    resp.headers['Access-Control-Max-Age'] = '86400'
    return resp


@app.route('/proxy/<path:endpoint>', methods=['OPTIONS'])
@app.route('/<path:_any>', methods=['OPTIONS'])
def cors_preflight(endpoint=None, _any=None):
    return ('', 204)


# ---------- Polymarket proxy ----------
# Maps path → upstream base URL. Forwards all query params, returns JSON + CORS.
PROXY_MAP = {
    'leaderboard':      'https://data-api.polymarket.com/v1/leaderboard',
    'activity':         'https://data-api.polymarket.com/activity',
    'trades':           'https://data-api.polymarket.com/trades',
    'positions':        'https://data-api.polymarket.com/positions',
    'closed-positions': 'https://data-api.polymarket.com/closed-positions',
    'value':            'https://data-api.polymarket.com/value',
    'markets':          'https://gamma-api.polymarket.com/markets',
}


@app.route('/proxy/claude', methods=['POST'])
def proxy_claude():
    """Proxy to Anthropic messages API using server-side ANTHROPIC_API_KEY.
    Body: {
      "prompt": "...",
      "max_tokens": 400,
      "model": "claude-sonnet-4-20250514",
      "web_search": true  // enables server-side web_search tool
    }
    Returns: {"text": "..."} on success, or {"error": "..."} on failure.
    """
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured on server'}), 503
    try:
        body = request.get_json(force=True, silent=True) or {}
        prompt = body.get('prompt', '')
        if not prompt:
            return jsonify({'error': 'prompt required'}), 400
        model = body.get('model', 'claude-sonnet-4-20250514')
        max_tokens = int(body.get('max_tokens', 400))
        web_search = bool(body.get('web_search', True))

        payload = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if web_search:
            payload['tools'] = [{
                'type': 'web_search_20250305',
                'name': 'web_search',
                'max_uses': 3,
            }]

        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            json=payload,
            timeout=45,  # web_search adds latency
        )
        if r.status_code != 200:
            log.warning('anthropic %d: %s', r.status_code, r.text[:300])
            return jsonify({'error': f'anthropic {r.status_code}', 'detail': r.text[:400]}), 502
        j = r.json()
        # With web_search, response contains multiple content blocks.
        # Concatenate all "text" blocks (skip server_tool_use / web_search_tool_result).
        text_parts = []
        for block in j.get('content', []) or []:
            if isinstance(block, dict) and block.get('type') == 'text':
                t = block.get('text', '').strip()
                if t:
                    text_parts.append(t)
        text = '\n\n'.join(text_parts).strip()
        return jsonify({'text': text or 'Analysis unavailable'}), 200
    except requests.exceptions.Timeout:
        return jsonify({'error': 'anthropic timeout'}), 504
    except Exception as e:
        log.exception('claude proxy failed')
        return jsonify({'error': str(e)}), 500


@app.route('/proxy/<endpoint>', methods=['GET'])
def proxy(endpoint):
    upstream = PROXY_MAP.get(endpoint)
    if not upstream:
        return jsonify({'error': 'unknown proxy endpoint', 'available': list(PROXY_MAP.keys())}), 404
    try:
        r = requests.get(upstream, params=request.args.to_dict(flat=False), timeout=15)
        # Forward upstream status code and body as JSON if possible, otherwise raw text
        ctype = r.headers.get('Content-Type', 'application/json')
        return Response(r.content, status=r.status_code, content_type=ctype)
    except requests.exceptions.Timeout:
        log.warning('proxy timeout: %s', upstream)
        return jsonify({'error': 'upstream timeout', 'upstream': upstream}), 504
    except Exception as e:
        log.exception('proxy failed for %s', endpoint)
        return jsonify({'error': str(e), 'upstream': upstream}), 502

# Lazily initialized singletons
_clob_client = None
_w3 = None


def get_clob_client():
    global _clob_client
    if _clob_client is None:
        from py_clob_client.client import ClobClient
        _clob_client = ClobClient(host='https://clob.polymarket.com', key=POLY_KEY, chain_id=137)
        _clob_client.set_api_creds(_clob_client.create_or_derive_api_creds())
    return _clob_client


def get_w3():
    global _w3
    if _w3 is None:
        from web3 import Web3
        _w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    return _w3


def require_token(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if API_TOKEN:
            sent = request.headers.get('X-API-Token', '')
            if sent != API_TOKEN:
                return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapped


@app.route('/', methods=['GET'])
def root():
    return jsonify({'service': 'polywatch-api', 'status': 'ok'}), 200


@app.route('/healthz', methods=['GET'])
def healthz():
    # Must respond fast and never touch external services
    return jsonify({'status': 'ok', 'poly_key_set': bool(POLY_KEY)}), 200


@app.route('/execute-trade', methods=['POST'])
@require_token
def execute_trade():
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType

        signal = request.get_json(force=True, silent=True) or {}
        amt = float(signal.get('approvedUsdc', 0) or 0)
        if amt < 10:
            return jsonify({'error': 'below minimum', 'min': 10}), 400
        token_id = signal.get('token_id', '')
        if not token_id:
            return jsonify({'error': 'token_id required'}), 400
        if not POLY_KEY:
            return jsonify({'error': 'POLY_KEY not configured'}), 500

        client = get_clob_client()
        order = client.create_order(OrderArgs(
            token_id=token_id,
            price=float(signal.get('price', 0.5)),
            size=round(amt, 2),
            side=signal.get('side', 'BUY'),
        ))
        result = client.post_order(order, OrderType.GTC)
        log.info('trade executed: %s', result)
        return jsonify({'ok': True, 'result': result}), 200
    except Exception as e:
        log.exception('execute-trade failed')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/withdraw', methods=['POST'])
@require_token
def withdraw():
    try:
        from web3 import Web3

        body = request.get_json(force=True, silent=True) or {}
        amt = float(body.get('amount', 0) or 0)
        to_address = body.get('to_address', '')
        if amt <= 0 or not to_address:
            return jsonify({'error': 'amount and to_address required'}), 400
        if not POLY_KEY:
            return jsonify({'error': 'POLY_KEY not configured'}), 500

        w3 = get_w3()
        to = Web3.to_checksum_address(to_address)
        usdc_addr = Web3.to_checksum_address(USDC_ADDRESS_STR)
        account = w3.eth.account.from_key(POLY_KEY)
        contract = w3.eth.contract(address=usdc_addr, abi=USDC_ABI)
        amount_wei = int(amt * 1_000_000)

        tx = contract.functions.transfer(to, amount_wei).build_transaction({
            'from': account.address,
            'nonce': w3.eth.get_transaction_count(account.address),
            'gas': 100000,
            'gasPrice': w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, POLY_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = tx_hash.hex()
        log.info('withdrawal sent: %s', tx_hex)
        return jsonify({'ok': True, 'tx_hash': tx_hex, 'amount': amt, 'to': to}), 200
    except Exception as e:
        log.exception('withdraw failed')
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    app.run(host='0.0.0.0', port=port)
