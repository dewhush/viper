#!/usr/bin/env python3
"""Graceful shutdown handler for Viper + Phantom"""
import signal, sys, os, json, time

_shutdown_handlers = []
_shutdown_triggered = False


def on_shutdown(callback):
    """Register a shutdown callback: callback() will be called on SIGTERM/SIGINT"""
    _shutdown_handlers.append(callback)


def is_shutting_down():
    return _shutdown_triggered


def _handle_signal(signum, frame):
    global _shutdown_triggered
    if _shutdown_triggered:
        return  # Don't re-trigger
    _shutdown_triggered = True
    sig_name = signal.Signals(signum).name
    print(f"\n[{time.strftime('%H:%M:%S')}] ⚠️ Received {sig_name} — shutting down gracefully...")
    for handler in _shutdown_handlers:
        try:
            handler()
        except Exception as e:
            print(f"Shutdown handler error: {e}")
    sys.exit(0)


def install():
    """Install SIGTERM + SIGINT handlers"""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


# Auto-install on import
install()
