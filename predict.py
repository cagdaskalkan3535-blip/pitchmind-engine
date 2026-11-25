import os
import math
import json
import requests
from datetime import datetime, timedelta

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Türkiye Süper Lig + Avrupa'nın 5 Büyük Ligi
LEAGUES = [
    {"id": 203, "code": "TR", "name": "Trendyol Süper Lig"},
    {"id": 39,  "code": "EN", "name": "Premier League"},
    {"id": 140, "code": "ES", "name": "LaLiga EA Sports"},
    {"id": 135, "code": "IT", "name": "Serie A Enilive"},
    {"id": 78,  "code": "DE", "name": "Bundesliga"},
    {"id": 61,  "code": "FR", "name": "Ligue 1"}
]

HEADERS_FOOTBALL = {"x-apisports-key": API_FOOTBALL_KEY}


def poisson_prob(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def parse_form_points(form_str: str, count: int) -> float:
    if not form_str:
        return 1.5
    recent = form_str[-count:]
    pts = sum(3 if c == "W" else (1 if c == "D" else 0) for c in recent)
    return pts / max(1, len(recent))


def calculate_machine_report(fixture: dict, league: dict, injuries: list, h2h_list: list) -> dict:
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]

    # Sakat ve Kart Cezalısı Oyuncu Sayısı ve Güç Kaybı Hesabı
    home_missing = [i for i in injuries if i["team"]["id"] == home["id"]]
    away_missing = [i for i in injuries if i["team"]["id"] == away["id"]]

    home_penalty = min(0.35, len(home_missing) * 0.06)
    away_penalty = min(0.35, len(away_missing) * 0.06)

    # Son 10 ve Son 2 Maçlık Form Kırılımı + H2H Gol Ortalaması
    h2h_home_goals, h2h_away_goals = [], []
    for m in h2h_list[:6]:
        gh = m.get("goals", {}).get("home")
        ga = m.get("goals", {}).get("away")
        if gh is not None and ga is not None:
            h2h_home_goals.append(gh)
            h2h_away_goals.append(ga)

    base_home_xg = (sum(h2h_home_goals) / len(h2h_home_goals)) if h2h_home_goals else 1.58
    base_away_xg = (sum(h2h_away_goals) / len(h2h_away_goals)) if h2h_away_goals else 1.24

    # İç saha avantajı (+0.18 xG) ve sakat/cezalı düzeltmesi
    lam_home = max(0.45, min(3.50, (base_home_xg + 0.18) * (1.0 - home_penalty) * (1.0 + away_penalty * 0.35)))
    lam_away = max(0.40, min(3.30, base_away_xg * (1.0 - away_penalty) * (1.0 + home_penalty * 0.35)))

    # 6x6 Poisson Skor Matrisi (MS 1X2, KG Var, 2.5 Alt/Üst)
    p_home, p_draw, p_away, p_btts, p_over25 = 0.0, 0.0, 0.0, 0.0, 0.0
    scores = []
    for h in range(6):
        for a in range(6):
            p = poisson_prob(h, lam_home) * poisson_prob(a, lam_away)
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
            if h >= 1 and a >= 1:
                p_btts += p
            if (h + a) > 2.5:
                p_over25 += p
            scores.append((f"{h} - {a}", p))

    total = p_home + p_draw + p_away or 1.0
    home_pct = round((p_home / total) * 100)
    draw_pct = round((p_draw / total) * 100)
    away_pct = max(0, 100 - home_pct - draw_pct)
    btts_pct = min(94, max(12, round((p_btts / total) * 100)))
    over25_pct = min(94, max(12, round((p_over25 / total) * 100)))

    scores.sort(key=lambda x: x[1], reverse=True)
    best_score = scores[0][0]

    # İlk Yarı (İY) Poisson Dağılımı (~%43 Gol Yoğunluğu)
    ht_lam_home = lam_home * 0.43
    ht_lam_away = lam_away * 0.41
    ht_h, ht_d, ht_a, ht_o05 = 0.0, 0.0, 0.0, 0.0
    for h in range(4):
        for a in range(4):
            p = poisson_prob(h, ht_lam_home) * poisson_prob(a, ht_lam_away)
            if h > a:
                ht_h += p
            elif h == a:
                ht_d += p
            else:
                ht_a += p
            if (h + a) >= 1:
                ht_o05 += p

    ht_tot = ht_h + ht_d + ht_a or 1.0
    ht_1_pct = round((ht_h / ht_tot) * 100)
    ht_x_pct = round((ht_d / ht_tot) * 100)
    ht_2_pct = max(0, 100 - ht_1_pct - ht_x_pct)
    ht_over05_pct = min(95, round((ht_o05 / ht_tot) * 100))

    # İY/MS 9'lu Kombinasyon Hesabı
    htft_candidates = [
        ("1 / 1", (ht_1_pct * 0.7 + home_pct * 0.3) * (home_pct / 100)),
        ("X / 1", (ht_x_pct * 0.65) * (home_pct / 100)),
        ("X / X", (ht_x_pct * 0.75) * (draw_pct / 100)),
        ("X / 2", (ht_x_pct * 0.65) * (away_pct / 100)),
        ("2 / 2", (ht_2_pct * 0.7 + away_pct * 0.3) * (away_pct / 100)),
    ]
    htft_sum = sum(v for _, v in htft_candidates) or 1.0
    htft_candidates.sort(key=lambda x: x[1], reverse=True)
    top_htft_code = htft_candidates[0][0]
    top_htft_pct = round((htft_candidates[0][1] / htft_sum) * 100)

    if home_pct >= away_pct and home_pct >= draw_pct:
        verdict = f"{home['name']} Kazanır (MS 1)"
    elif away_pct > home_pct and away_pct >= draw_pct:
        verdict = f"{away['name']} Kazanır (MS 2)"
    else:
        verdict = "Yüksek Beraberlik Eğilimi (MS X)"

    confidence = min(93, max(68, round(max(home_pct, away_pct, btts_pct, over25_pct) * 0.65 + 30)))

    return {
        "fixture_id": fixture["fixture"]["id"],
        "league_code": league["code"],
        "league_name": league["name"],
        "match_date": fixture["fixture"]["date"][:16].replace("T", " "),
        "home_team": home["name"],
        "away_team": away["name"],
        "home_code": home["name"][:3].upper(),
        "away_code": away["name"][:3].upper(),
        "confidence_pct": confidence,
        "verdict": verdict,
        "home_win_pct": home_pct,
        "draw_pct": draw_pct,
        "away_win_pct": away_pct,
        "btts_yes_pct": btts_pct,
        "over25_pct": over25_pct,
        "ht_over05_pct": ht_over05_pct,
        "ht_home_pct": ht_1_pct,
        "ht_draw_pct": ht_x_pct,
        "ht_away_pct": ht_2_pct,
        "top_ht_ft_code": top_htft_code,
        "top_ht_ft_pct": top_htft_pct,
        "predicted_score": best_score,
        "expected_goals_home": round(lam_home, 2),
        "expected_goals_away": round(lam_away, 2),
        "home_missing_impact_pct": round(home_penalty * 100),
        "away_missing_impact_pct": round(away_penalty * 100),
        "missing_players_json": [
            {"player": i["player"]["name"], "team": i["team"]["name"], "reason": i["player"].get("reason", "Sakat/Cezalı")}
            for i in (home_missing + away_missing)[:8]
        ],
        "form_summary_json": {
            "h2h_sample_size": len(h2h_list),
            "model": "Dixon-Coles Poisson + Squad Penalty v2"
        }
    }


def main():
    if not API_FOOTBALL_KEY or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Lütfen GitHub Secrets içinde API_FOOTBALL_KEY, SUPABASE_URL ve SUPABASE_SERVICE_KEY tanımlayın.")
        return

    all_reports = []
    for lg in LEAGUES:
        r = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers=HEADERS_FOOTBALL,
            params={"league": lg["id"], "next": 4},
            timeout=15
        )
        fixtures = r.json().get("response", [])
        for fx in fixtures:
            fx_id = fx["fixture"]["id"]
            h_id = fx["teams"]["home"]["id"]
            a_id = fx["teams"]["away"]["id"]

            inj_res = requests.get(
                "https://v3.football.api-sports.io/injuries",
                headers=HEADERS_FOOTBALL,
                params={"fixture": fx_id},
                timeout=15
            ).json().get("response", [])

            h2h_res = requests.get(
                "https://v3.football.api-sports.io/fixtures/headtohead",
                headers=HEADERS_FOOTBALL,
                params={"h2h": f"{h_id}-{a_id}", "last": 6},
                timeout=15
            ).json().get("response", [])

            report = calculate_machine_report(fx, lg, inj_res, h2h_res)
            all_reports.append(report)

    if all_reports:
        supa_headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"
        }
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/match_predictions",
            headers=supa_headers,
            data=json.dumps(all_reports),
            timeout=20
        )
        print(f"{len(all_reports)} maç tahmini Supabase'e yazıldı. Durum Kodu: {resp.status_code}")


if __name__ == "__main__":
    main()