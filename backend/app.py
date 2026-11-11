"""
Backend do MVP UberBus (FastAPI).

Rotas:
  GET /                      -> página de teste (mapa)
  GET /api/geocode?q=...     -> endereço -> coordenadas (Nominatim/OSM, gratuito)
  GET /api/rota?...          -> planeja a viagem de ônibus (OpenTripPlanner local)
  GET /api/saude             -> verifica se o OTP está no ar

Rodar:  .venv\\Scripts\\uvicorn backend.app:app --reload
"""
from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

OTP_URL = os.getenv("OTP_URL", "http://localhost:8080/otp/gtfs/v1")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# A política do Nominatim exige um User-Agent que identifique a aplicação.
USER_AGENT = os.getenv("USER_AGENT", "UberBus-MVP/0.1")
TZ = ZoneInfo("America/Sao_Paulo")
# Caixa de Porto Alegre (oeste, norte, leste, sul), usada para priorizar resultados locais.
POA_VIEWBOX = "-51.31,-29.93,-51.01,-30.27"

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="UberBus MVP")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

PLAN_QUERY = """
query Plano($origem: PlanLabeledLocationInput!, $destino: PlanLabeledLocationInput!,
            $quando: OffsetDateTime!, $n: Int!) {
  planConnection(
    origin: $origem
    destination: $destino
    dateTime: { earliestDeparture: $quando }
    first: $n
    modes: { transit: { transit: [{ mode: BUS }] } }
    preferences: {
      # Em POA cada baldeação normalmente é uma passagem nova: penaliza (600 ≈ 10 min)
      # e dá 2 min de folga para trocar de ônibus.
      transit: { transfer: { cost: 600, slack: "PT2M", maximumTransfers: 2 } }
    }
  ) {
    edges {
      node {
        start
        end
        duration
        walkDistance
        legs {
          mode
          start { scheduledTime }
          end { scheduledTime }
          duration
          distance
          headsign
          from { name lat lon }
          to { name lat lon }
          route { shortName longName color }
          legGeometry { points }
          intermediatePlaces { name }
        }
      }
    }
  }
}
"""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/saude")
async def saude() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(OTP_URL, json={"query": "{ feeds { feedId } }"})
            r.raise_for_status()
        return {"otp": "ok", "feeds": r.json()["data"]["feeds"]}
    except Exception as e:  # noqa: BLE001
        return {"otp": "fora do ar", "erro": str(e)}


@lru_cache(maxsize=512)
def _geocode(q: str) -> list[dict]:
    params = {
        "q": q, "format": "jsonv2", "limit": 5, "countrycodes": "br",
        "viewbox": POA_VIEWBOX, "bounded": 1, "accept-language": "pt-BR",
    }
    r = httpx.get(NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=10)
    r.raise_for_status()
    return [{"nome": x["display_name"], "lat": float(x["lat"]), "lon": float(x["lon"])} for x in r.json()]


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=3)) -> list[dict]:
    try:
        return _geocode(q.strip())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Erro no Nominatim: {e}") from e


def _ms_para_hora(iso: str | None) -> str | None:
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M") if iso else None


@app.get("/api/rota")
async def rota(
    de_lat: float, de_lon: float, para_lat: float, para_lon: float,
    quando: datetime | None = Query(None, description="ISO 8601; padrão = agora"),
    n: int = Query(3, ge=1, le=6),
) -> dict:
    quando = (quando or datetime.now(TZ))
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=TZ)
    variaveis = {
        "origem": {"location": {"coordinate": {"latitude": de_lat, "longitude": de_lon}}},
        "destino": {"location": {"coordinate": {"latitude": para_lat, "longitude": para_lon}}},
        "quando": quando.isoformat(),
        "n": n,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(OTP_URL, json={"query": PLAN_QUERY, "variables": variaveis})
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"OpenTripPlanner indisponível: {e}") from e
    corpo = r.json()
    if corpo.get("errors"):
        raise HTTPException(502, corpo["errors"])

    opcoes = []
    for edge in corpo["data"]["planConnection"]["edges"]:
        it = edge["node"]
        so_a_pe = all(leg["mode"] == "WALK" for leg in it["legs"])
        if so_a_pe and it["walkDistance"] > 1500:
            continue  # o OTP às vezes sugere caminhar vários km; não faz sentido num app de ônibus
        opcoes.append({
            "saida": _ms_para_hora(it["start"]),
            "chegada": _ms_para_hora(it["end"]),
            "duracao_min": round(it["duration"] / 60),
            "caminhada_m": round(it["walkDistance"]),
            "trechos": [{
                "modo": "ONIBUS" if leg["mode"] == "BUS" else "A_PE" if leg["mode"] == "WALK" else leg["mode"],
                "saida": _ms_para_hora(leg["start"]["scheduledTime"]),
                "chegada": _ms_para_hora(leg["end"]["scheduledTime"]),
                "duracao_min": round(leg["duration"] / 60),
                "distancia_m": round(leg["distance"]),
                "de": leg["from"], "para": leg["to"],
                "linha": leg["route"]["shortName"] if leg["route"] else None,
                "nome_linha": leg["route"]["longName"] if leg["route"] else None,
                "cor": leg["route"]["color"] if leg["route"] else None,
                "sentido": leg["headsign"],
                "paradas": len(leg["intermediatePlaces"] or []),
                "geometria": leg["legGeometry"]["points"],  # polyline codificada (Google)
            } for leg in it["legs"]],
        })
    return {"quando": quando.isoformat(), "opcoes": opcoes}
