import ccxt

api_key = "toh5k8mN7CZzzs5ee9"
exchange = ccxt.bybit({
    'apiKey': api_key,
    'enableRateLimit': True,
})

try:
    balance = exchange.fetch_balance()
    print("Balance:", balance)
except Exception as e:
    print("Error:", e)
