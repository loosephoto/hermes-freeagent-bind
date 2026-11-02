#!/usr/bin/env python3
"""List or explicitly reconcile an inactive token reservation.

Read-only by default. A write requires both --confirm-inactive and --apply.
The operator must first ensure the request is no longer running; a reservation
cannot reliably tell a crashed request from a still-running process.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from freeagent_bind import server as S

for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8', errors='replace')


def list_reservations() -> dict:
    rows = S._quota_read(S.quota_path())
    out = []
    for provider, row in sorted(rows.items()):
        pending = row.get('pending', {})
        if not isinstance(pending, dict):
            raise ValueError(f'{provider}: 予約の形式が不正です')
        for reservation_id, info in sorted(pending.items()):
            if not isinstance(info, dict):
                raise ValueError(f'{provider}: 予約の形式が不正です')
            tokens = info.get('tokens')
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError(f'{provider}: 予約トークン数が不正です')
            out.append({'provider': provider, 'reservation_id': reservation_id,
                        'estimated_tokens': tokens, 'created_at': info.get('at')})
    return {'reservations': out}


def _reservation(rows: dict, provider: str, reservation_id: str) -> dict:
    row = rows.get(provider)
    pending = row.get('pending') if isinstance(row, dict) else None
    info = pending.get(reservation_id) if isinstance(pending, dict) else None
    if not isinstance(info, dict):
        raise ValueError('指定された予約がありません')
    estimate = info.get('tokens')
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
        raise ValueError('予約トークン数が不正です')
    return info


def settle(provider: str, reservation_id: str, actual: int | None, apply: bool) -> dict:
    path = S.quota_path()
    if not apply:
        info = _reservation(S._quota_read(path), provider, reservation_id)
        return {'provider': provider, 'reservation_id': reservation_id, 'applied': False,
                'charged_tokens': info['tokens'] if actual is None else actual,
                'basis': 'estimate' if actual is None else 'operator',
                'note': 'プレビューです。適用時に状態を再確認します。'}
    # The operator attests that the request is no longer running. Hold the same
    # cross-process lock as normal reservations; never replace an old snapshot.
    with S._file_lock(path) as locked:
        if not locked:
            raise OSError('クォータのロックを取得できませんでした')
        with S._QUOTA_LOCK:
            rows = S._quota_read(path)
            info = _reservation(rows, provider, reservation_id)
            charged = info['tokens'] if actual is None else actual
            S._QUOTA.clear()
            S._QUOTA.update(rows)
            now = S.now_ts()
            row = S._quota_row(provider, now)
            del row['pending'][reservation_id]
            if charged:
                # Account at reconciliation time, conservatively retaining the
                # estimate for a fresh 60-second window and the current UTC day.
                row['minute_events'].append([now, 0, charged])
                row['min_tok'] = int(row.get('min_tok') or 0) + charged
                row['day_tok'] = int(row.get('day_tok') or 0) + charged
            if not S._quota_write(S._QUOTA):
                raise OSError('クォータの精算結果を保存できませんでした')
    return {'provider': provider, 'reservation_id': reservation_id, 'applied': True,
            'charged_tokens': charged, 'basis': 'estimate' if actual is None else 'operator',
            'note': '使用量は提供元の正確な値を保証しません。'}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--settle', nargs=2, metavar=('PROVIDER', 'RESERVATION_ID'))
    ap.add_argument('--actual-tokens', type=int, help='確認済みの実使用量。未指定なら予約量を保守的に計上')
    ap.add_argument('--confirm-inactive', action='store_true', help='対象の処理が完了または停止したことを確認')
    ap.add_argument('--apply', action='store_true', help='確認結果を実際に保存（省略時は表示のみ）')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    if args.actual_tokens is not None and args.actual_tokens < 0:
        ap.error('--actual-tokens は 0 以上にしてください')
    if args.settle and not args.confirm_inactive:
        ap.error('実行中でないことを確認し --confirm-inactive を指定してください')
    if not args.settle and (args.actual_tokens is not None or args.confirm_inactive or args.apply):
        ap.error('--actual-tokens / --confirm-inactive / --apply は --settle と一緒に指定してください')
    try:
        data = (settle(args.settle[0], args.settle[1], args.actual_tokens, args.apply)
                if args.settle else list_reservations())
    except (OSError, ValueError, S.QuotaExhaustedError) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
