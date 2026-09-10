"""장중 잡 (목표가·손절 체크 / 급등 감지 / 추적 종목 타이밍)

main.py 에서 옮겨왔다. 등록과 실행 순서는 main.setup_schedule 이 계속 관장한다.
"""
import time
from loguru import logger
from src.notifier.discord import DiscordNotifier
from src.utils.trailing_stop import get_trailing_manager
from src.utils.sell_signal import SellSignalDetector
from src.utils.sell_signal import load_holdings
from src.utils.watchlist import get_watchlist_manager
from src.analyzers.surge_detector import SurgeDetector
from src.utils.session_state import sync_today_picks as _sync_today_picks
from src.utils.session_state import mark_realtime_alert as _mark_realtime_alert




def run_realtime_check():
    """장중 실시간 목표가/손절/트레일링 체크"""
    from src.api.kis_client import KISClient

    if not _sync_today_picks():
        return
    kis = KISClient()
    if not kis.is_market_open():
        return

    notifier = DiscordNotifier()
    tm = get_trailing_manager()

    # ── 트레일링 스탑 체크 ───────────────────────────────
    try:
        def _get_price(ticker: str) -> int:
            return kis.get_stock_price(ticker)["price"]

        signals = tm.check_all(_get_price)
        for sig in signals:
            stype = sig.get("signal")
            name  = sig.get("name", "")
            price = sig.get("current_price", 0)
            gain  = sig.get("gain_pct", 0)

            if stype == "partial_exit_1":
                sell_pct  = sig.get("sell_pct", 50)
                new_stop  = sig.get("new_stop", 0)
                next_tgt  = sig.get("next_target", 0)
                notifier._send({"embeds": [{
                    "title": f"✂️ 1차 분할 익절: {name}",
                    "description": (
                        f"현재가: **{price:,}원** (+{gain:.1f}%)\n"
                        f"💰 **{sell_pct}% 매도 권장** (보유분의 절반)\n"
                        f"손절가 → 진입가({new_stop:,}원)로 상향 (본전 손절)\n"
                        f"2차 익절 목표: `{next_tgt:,}원` (+8%)"
                    ),
                    "color": 0x00C851,
                }]})

            elif stype == "partial_exit_2":
                sell_pct   = sig.get("sell_pct", 30)
                trail_stop = sig.get("trailing_stop", 0)
                notifier._send({"embeds": [{
                    "title": f"✂️ 2차 분할 익절: {name}",
                    "description": (
                        f"현재가: **{price:,}원** (+{gain:.1f}%)\n"
                        f"💰 **{sell_pct}% 추가 매도 권장**\n"
                        f"나머지 20% → 트레일링 스탑 전환\n"
                        f"트레일링 손절가: `{trail_stop:,}원` (고점 -3%)\n"
                        f"🚀 대박 수익을 노려요!"
                    ),
                    "color": 0x4CAF50,
                }]})

            elif stype == "trailing_activated":
                trail_stop = sig.get("trailing_stop", 0)
                notifier._send({"embeds": [{
                    "title": f"🔄 트레일링 활성화: {name}",
                    "description": (
                        f"현재가: **{price:,}원** (+{gain:.1f}%)\n"
                        f"익절가 도달 → 트레일링 모드 전환\n"
                        f"트레일링 손절가: `{trail_stop:,}원` (고점 -3%)\n"
                        f"추가 수익을 노리며 보유 계속"
                    ),
                    "color": 0x00C851,
                }]})

            elif stype == "trailing_stop":
                high    = sig.get("high_price", 0)
                max_pct = sig.get("max_pct", 0)
                notifier._send({"embeds": [{
                    "title": f"🎯 트레일링 스탑 발동: {name}",
                    "description": (
                        f"현재가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"고점: {high:,}원 (+{max_pct:.1f}%)\n"
                        f"고점 대비 -3% 하락 → 나머지 20% 매도\n"
                        f"💡 분할 익절 전략 완료!"
                    ),
                    "color": 0x2196F3,
                }]})

                # [약점 7번] 청산 후 추적 종목으로 자동 전환
                # 강한 종목은 재조정 후 재진입 기회 포착
                try:
                    wm = get_watchlist_manager()
                    # 청산가를 기준으로 추적 등록 (재조정 기다림)
                    _pick_ref = next(
                        (p for p in _sync_today_picks() if p.get("name") == name), {}
                    )
                    _score = _pick_ref.get("final_score", 0)
                    if _score >= 5.0:  # 점수 좋은 종목만 재추적
                        wm.add(
                            ticker=sig.get("ticker", ""),
                            name=name,
                            score=_score,
                            entry_price=price,
                            reason=f"청산 후 재추적 (최고 +{max_pct:.1f}%)"
                        )
                        notifier._send({"embeds": [{
                            "title": f"👀 재추적 등록: {name}",
                            "description": (
                                f"청산 완료 후 재조정 기다리는 중\n"
                                f"등록가: {price:,}원 | 목표진입: {int(price*0.97):,}원\n"
                                f"조정 + 반등 시 자동 알림"
                            ),
                            "color": 0x9C27B0,
                        }]})
                except Exception as e:
                    logger.debug(f"재추적 등록 오류: {e}")

            elif stype == "stop_warning":
                stop  = sig.get("stop_loss", 0)
                notifier._send({"embeds": [{
                    "title": f"⚠️ 손절 경고 (장중): {name}",
                    "description": (
                        f"현재가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"손절가: {stop:,}원 터치\n"
                        f"📌 아직 매도 X → 종가 확인 후 판단\n"
                        f"14:30 이후에도 손절가 하회 시 매도 권고"
                    ),
                    "color": 0xFF9800,
                }]})

            elif stype == "stop_loss":
                reason = sig.get("reason", "손절가 도달")
                notifier._send({"embeds": [{
                    "title": f"🛑 손절 신호: {name}",
                    "description": (
                        f"현재가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"{reason} → 매도 신호"
                    ),
                    "color": 0xFF4444,
                }]})

            elif stype == "stop_loss_close":
                stop = sig.get("stop_loss", 0)
                notifier._send({"embeds": [{
                    "title": f"🛑 종가 손절 확정: {name}",
                    "description": (
                        f"종가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"손절가 {stop:,}원 하회 마감\n"
                        f"📌 내일 시초가에 매도 권고"
                    ),
                    "color": 0xFF4444,
                }]})

    except Exception as e:
        logger.debug(f"트레일링 체크 오류: {e}")

    # ── 기존 실시간 체크 (트레일링 미활성 종목) ───────────
    for pick in _sync_today_picks():
        try:
            ticker  = pick["ticker"]
            current = kis.get_stock_price(ticker)
            price   = current["price"]
            entry   = pick.get("entry_strategy", {}).get("split_1st", pick.get("price", 0))
            change  = (price - entry) / entry * 100 if entry else 0

            # 트레일링 활성 종목은 트레일링이 관리
            if tm.stops.get(ticker, {}).get("trailing_active"):
                continue

            es     = pick.get("entry_strategy", {})
            target = es.get("target_price", 0) or pick.get("ai_eval", {}).get("target_price", 0)
            stop   = es.get("stop_loss", 0) or pick.get("ai_eval", {}).get("stop_loss", 0)

            if target and price >= target:
                if _mark_realtime_alert(ticker, "target"):
                    notifier.send_realtime_alert(
                        {**pick, "price": price, "change_rate": change},
                        f"🎯 목표가 도달! ({price:,}원) +{change:.1f}%"
                    )
            elif stop and price <= stop:
                if _mark_realtime_alert(ticker, "stop"):
                    notifier.send_realtime_alert(
                        {**pick, "price": price, "change_rate": change},
                        f"🛑 손절 라인 도달! ({price:,}원) {change:.1f}%"
                    )
            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"실시간 체크 오류 ({pick.get('ticker','')}): {e}")

    # ── 보유 종목 매도 신호 감지 ─────────────────────────
    try:
        holdings = load_holdings()
        if holdings:
            detector = SellSignalDetector()
            for ticker, holding in holdings.items():
                try:
                    candles  = kis.get_daily_ohlcv(ticker, days=30)
                    investor = kis.get_investor_trend(ticker, days=5)
                    signals  = detector.detect(ticker, holding, candles, investor)

                    if not signals:
                        continue

                    severity = detector.evaluate_severity(signals)
                    if severity == "none":
                        continue

                    name = holding.get("name", ticker)
                    color = {
                        "high":   0xFF4444,
                        "medium": 0xFF9800,
                        "low":    0xFFD700,
                    }.get(severity, 0xFFD700)

                    signal_lines = []
                    for s in signals:
                        dot = "🔴" if s["severity"]=="high" else ("🟡" if s["severity"]=="medium" else "🔵")
                        signal_lines.append(f"{dot} **{s['message']}** → {s['action']}")
                    signal_text = "\n".join(signal_lines)

                    entry = holding.get("entry_price", 0)
                    current_price = candles[-1]["close"] if candles else 0
                    gain_pct = (current_price - entry) / entry * 100 if entry else 0

                    notifier._send({"embeds": [{
                        "title": f"⚠️ 매도 신호 감지: {name} ({ticker})",
                        "description": (
                            f"현재가: **{current_price:,}원** ({gain_pct:+.1f}%)\n"
                            f"진입가: {entry:,}원\n\n"
                            f"{signal_text}"
                        ),
                        "color": color,
                        "footer": {"text": f"신호 {len(signals)}개 감지 | 심각도: {severity}"},
                    }]})
                    logger.info(f"매도 신호 알림: {name} ({len(signals)}개)")
                    time.sleep(0.3)

                except Exception as e:
                    logger.debug(f"매도 신호 감지 오류 ({ticker}): {e}")

    except Exception as e:
        logger.debug(f"보유 종목 매도 신호 전체 오류: {e}")




def run_surge_detection():
    """장중 실시간 급등 감지 (5분마다)"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    if not kis.is_market_open():
        return

    notifier = DiscordNotifier()
    detector = SurgeDetector()

    try:
        # 오늘 이미 추천된 종목 제외
        exclude = {p["ticker"] for p in _sync_today_picks()}

        # 추적 종목도 제외 (중복 알림 방지)
        try:
            wm = get_watchlist_manager()
            exclude |= {item["ticker"] for item in wm.get_all()}
        except Exception:
            pass

        signals = detector.detect(kis, exclude_tickers=exclude)

        for sig in signals:
            name      = sig.get("name", "")
            ticker    = sig.get("ticker", "")
            price     = sig.get("price", 0)
            change    = sig.get("change_rate", 0)
            intraday  = sig.get("intraday_change", 0)
            vol_ratio = sig.get("volume_ratio", 0)
            trade_val = sig.get("trade_value_billion", 0)
            foreign   = sig.get("foreign_net", 0)
            inst      = sig.get("inst_net", 0)
            strength  = sig.get("strength", "medium")

            strength_emoji = "🔥" if strength == "strong" else "⚡"

            supply_text = ""
            if foreign > 0 and inst > 0:
                supply_text = f"외국인+기관 동반매수 ({foreign:+,}/{inst:+,})"
            elif foreign > 0:
                supply_text = f"외국인 매수 ({foreign:+,})"
            elif inst > 0:
                supply_text = f"기관 매수 ({inst:+,})"
            else:
                supply_text = "수급 미확인"

            notifier._send({"embeds": [{
                "title": f"{strength_emoji} 실시간 급등: {name} ({ticker})",
                "description": (
                    f"현재가: **{price:,}원** ({change:+.2f}%)\n"
                    f"시초가 대비: +{intraday:.2f}% (장중 모멘텀)\n"
                    f"거래량: 평균 대비 **{vol_ratio:.1f}배**\n"
                    f"거래대금: {trade_val:.0f}억원\n"
                    f"📊 {supply_text}\n\n"
                    f"💡 강도: **{strength.upper()}**"
                ),
                "color": 0xFF6F00 if strength == "strong" else 0xFFA000,
                "footer": {"text": f"실시간 감지 | {sig.get('detected_at')}"},
            }]})
            logger.info(f"급등 알림: {name} ({change:+.2f}%, 거래량 {vol_ratio:.1f}배)")

    except Exception as e:
        logger.debug(f"급등 감지 오류: {e}")




def run_watchlist_check():
    """장중 추적 종목 매수 타이밍 체크 (30분마다)"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    if not kis.is_market_open():
        return

    notifier = DiscordNotifier()
    wm = get_watchlist_manager()

    try:
        # 만료 종목 정리
        wm.clean_expired()

        # 타이밍 체크
        signals = wm.check_all(kis)

        for sig in signals:
            name        = sig.get("name", "")
            ticker      = sig.get("ticker", "")
            current     = sig.get("current", 0)
            pullback    = sig.get("pullback_pct", 0)
            rsi         = sig.get("rsi", 0)
            score       = sig.get("score", 0)
            reg_price   = sig.get("reg_price", 0)
            tgt_entry   = sig.get("target_entry", 0)
            is_bullish  = sig.get("is_bullish", False)
            reason      = sig.get("reason", "")
            bull_emoji  = "🕯️ 양봉" if is_bullish else "📉 음봉"

            notifier._send({"embeds": [{
                "title": f"🎯 매수 타이밍: {name} ({ticker})",
                "description": (
                    f"**등록 이유:** {reason}\n"
                    f"등록가: {reg_price:,}원 → 현재: **{current:,}원** "
                    f"({pullback:+.1f}% 조정)\n\n"
                    f"📊 RSI: {rsi:.1f} | {bull_emoji}\n"
                    f"✅ 눌림목 + 수급 유지 조건 충족\n\n"
                    f"💡 **권장 진입가: {tgt_entry:,}원**\n"
                    f"종합점수: {score:.1f}점"
                ),
                "color": 0x9C27B0,
                "footer": {"text": "추적 종목 매수 타이밍 알림"},
            }]})
            logger.info(f"추적 타이밍 알림: {name} ({pullback:+.1f}% 조정, RSI={rsi:.1f})")

    except Exception as e:
        logger.debug(f"추적 체크 오류: {e}")
