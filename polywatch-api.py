#!/usr/bin/env python3
"""
PolyWatch API — HTTP wrapper for execute_trade.py and withdraw.py logic.
Deployed to Railway as a separate service so n8n (also on Railway) can call it.

Endpoints:
  GET  /healthz                 → liveness
  POST /execute-trade  body: JSON signal (approvedUsdc, token_id, price, side, outcome, market)
  POST /withdraw       body: {"amount": float, "to_address": "0x..."}

Env vars required:
  POLY_KEY   — Polymarket proxy wallet private key
  API_TOKEN  — shared secret, sent by n8n in X-API-Token header (optional but recommended)
"""
import os
import json
import logging
from functools import wraps

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from web3 import Web3

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('polywatch-api')

POLY_KEY = os.getenv('POLY_KEY')
API_TOKEN = os.getenv('API_TOKEN', '')
POLYGON_RPC = os.getenv('POLYGON_RPC', 'https://polygon-rpc.com')
USDC_ADDRESS = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
USDC_ABI = '[{"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}]'

if not POLY_KEY:
    log.warning('POLY_KEY not set — trade and withdraw endpoints will fail')

app = Flask(__name__)

# Lazily initialized singletons
_clob_client = None
_w3 = None


def get_clob_client():
    global _clob_client
    if _clob_client is None:
        _clob_client = ClobClient(host='https://clob.polymarket.com', key=POLY_KEY, chain_id=137)
        _clob_client.set_api_creds(_clob_client.create_or_derive_api_creds())
    return _clob_client


def get_w3():
    global _w3
    if _w3 is None:
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


@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({'status': 'ok', 'poly_key_set': bool(POLY_KEY)}), 200


@app.route('/execute-trade', methods=['POST'])
@require_token
def execute_trade():
    try:
        signal = request.get_json(force=True, silent=True) or {}
        amt = float(signal.get('approvedUsdc', 0) or 0)
        if amt < 10:
            return jsonify({'error': 'below minimum', 'min': 10}), 400
        token_id = signal.get('token_id', '')
        if not token_id:
            return jsonify({'error': 'token_id required'}), 400

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
        body = request.get_json(force=True, silent=True) or {}
        amt = float(body.get('amount', 0) or 0)
        to_address = body.get('to_address', '')
        if amt <= 0 or not to_address:
            return jsonify({'error': 'amount and to_address required'}), 400

        w3 = get_w3()
        to = Web3.to_checksum_address(to_address)
        account = w3.eth.account.from_key(POLY_KEY)
        contract = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
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
