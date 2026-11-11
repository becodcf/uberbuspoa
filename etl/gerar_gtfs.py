"""
Gera um GTFS atualizado de Porto Alegre combinando:
  - GTFS antigo da EPTC (paradas com coordenadas, traçados, sequência de paradas, duração das viagens)
  - CSVs novos da EPTC (tabela horária atual)

Para cada horário de saída do CSV novo, a viagem antiga mais parecida (mesma linha, mesmo
sentido, horário mais próximo, de preferência no mesmo tipo de dia) é clonada: herda paradas,
shape e duração, mas recebe o novo horário de saída. Os horários intermediários ficam vazios
(como no GTFS original) e são interpolados pelo OpenTripPlanner.

Uso:  python etl/gerar_gtfs.py
Saída: otp/gtfs-poa.zip  e  etl/relatorio.txt
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
GTFS_ANTIGO = DADOS / "arquivo-gtfs.zip"
CSV_HORARIOS = DADOS / "tabela_horaria_onibus.csv"
SAIDA = RAIZ / "otp" / "gtfs-poa.zip"
RELATORIO = RAIZ / "etl" / "relatorio.txt"

INICIO = date(2026, 11, 1)
FIM = date(2027, 10, 31)

# Tabela usada por linha, em ordem de prioridade. Tabelas sazonais (VERAO, CARNAVAL,
# PASSELIVRE...) ficam de fora do MVP.
PRIORIDADE_TABELA = ["OFICIAL", "EVENTUAL"]

# Feriados (nacionais + RS + Porto Alegre) no período: usam a tabela de FERIADO.
FERIADOS = [
    "2026-11-02", "2026-11-15", "2026-11-20", "2026-12-25",
    "2027-01-01", "2027-02-02", "2027-03-26", "2027-04-21", "2027-05-01",
    "2027-05-27", "2027-09-07", "2027-09-20", "2027-10-12",
]

# tipo_dia do CSV -> service_id e dias da semana (seg..dom)
SERVICOS = {
    "DIAUTIL": ("DU", [1, 1, 1, 1, 1, 0, 0]),
    "SABADO": ("SAB", [0, 0, 0, 0, 0, 1, 0]),
    "DOMINGO": ("DOM", [0, 0, 0, 0, 0, 0, 1]),
    "FERIADO": ("FER", [0, 0, 0, 0, 0, 0, 0]),  # só via calendar_dates
}
# sufixo do service_id antigo (@1/@2/@3) -> tipo_dia
DIA_ANTIGO = {"1": "DIAUTIL", "2": "SABADO", "3": "DOMINGO"}


def hms_para_seg(s: str) -> int:
    partes = [int(p) for p in s.split(":")]
    while len(partes) < 3:
        partes.append(0)
    h, m, sec = partes
    return h * 3600 + m * 60 + sec


def seg_para_hms(x: int) -> str:
    return f"{x // 3600:02d}:{x % 3600 // 60:02d}:{x % 60:02d}"


VELOCIDADE_PADRAO_KMH = 18  # média aproximada de ônibus urbano, usada só como último recurso


def comprimento_shapes_km(shapes: pd.DataFrame) -> pd.Series:
    s = shapes.copy()
    s["shape_pt_sequence"] = s["shape_pt_sequence"].astype(int)
    s = s.sort_values(["shape_id", "shape_pt_sequence"])
    lat = np.radians(s["shape_pt_lat"].astype(float).to_numpy())
    lon = np.radians(s["shape_pt_lon"].astype(float).to_numpy())
    dlat, dlon = np.diff(lat, prepend=lat[0]), np.diff(lon, prepend=lon[0])
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(np.roll(lat, 1)) * np.sin(dlon / 2) ** 2
    d = 2 * 6371 * np.arcsin(np.sqrt(a))
    d[(s["shape_id"] != s["shape_id"].shift()).to_numpy()] = 0  # não somar entre shapes
    return pd.Series(d, index=s.index).groupby(s["shape_id"]).sum()


def corrigir_duracoes(trips: pd.DataFrame, shapes: pd.DataFrame, log: list[str]) -> None:
    """O GTFS antigo tem viagens com chegada == saída (ou absurdamente curtas).
    Troca essas durações pela mediana das viagens válidas do mesmo padrão; na falta,
    estima pelo comprimento do traçado."""
    minimo = np.maximum(300, trips["n_paradas"] * 20)  # >= 5 min e >= 20 s por parada
    valida = trips["duracao"] >= minimo
    trips.loc[~valida, "duracao"] = np.nan
    for chave in (["linha", "sentido", "shape_id"], ["linha", "sentido"], ["linha"]):
        trips["duracao"] = trips["duracao"].fillna(trips.groupby(chave)["duracao"].transform("median"))
    km = trips["shape_id"].map(comprimento_shapes_km(shapes))
    trips["duracao"] = trips["duracao"].fillna(km / VELOCIDADE_PADRAO_KMH * 3600).fillna(1800).astype(int)
    log.append(f"Viagens antigas com duração inválida corrigidas: {int((~valida).sum())} de {len(trips)}")


def ler_gtfs(caminho: Path) -> dict[str, pd.DataFrame]:
    with zipfile.ZipFile(caminho) as z:
        return {
            n[:-4]: pd.read_csv(z.open(n), dtype=str, keep_default_na=False, encoding="utf-8", encoding_errors="replace")
            for n in z.namelist() if n.endswith(".txt")
        }


def main() -> None:
    log: list[str] = []
    g = ler_gtfs(GTFS_ANTIGO)
    routes, trips, st = g["routes"], g["trips"], g["stop_times"]

    # ---------- viagens antigas: saída, duração e tipo de dia ----------
    st["stop_sequence"] = st["stop_sequence"].astype(int)
    st = st.sort_values(["trip_id", "stop_sequence"])
    com_hora = st[st["departure_time"] != ""]
    ini = com_hora.groupby("trip_id").first()
    fim = com_hora.groupby("trip_id").last()
    info = pd.DataFrame({
        "saida": ini["departure_time"].map(hms_para_seg),
        "chegada": fim["arrival_time"].map(hms_para_seg),
    })
    info["duracao"] = info["chegada"] - info["saida"]
    info["n_paradas"] = st.groupby("trip_id").size()
    trips = trips.merge(info, left_on="trip_id", right_index=True)
    trips = trips.merge(routes[["route_id", "route_short_name"]], on="route_id")
    trips["linha"] = trips["route_short_name"].str.upper().str.strip()
    trips["sentido"] = trips["trip_headsign"].str.upper().str.strip()
    trips["tipo_dia"] = trips["service_id"].str.split("@").str[-1].map(DIA_ANTIGO)
    corrigir_duracoes(trips, g["shapes"], log)

    # ---------- horários novos ----------
    h = pd.read_csv(CSV_HORARIOS, sep=";", dtype=str, encoding="utf-8", encoding_errors="replace")
    h["linha"] = h["linha"].str.upper().str.strip()
    h["sentido"] = h["sentido"].str.upper().str.strip()
    tabelas_por_linha = h.groupby("linha")["tipo_tabela"].apply(set)
    escolha = {}
    for linha, tabs in tabelas_por_linha.items():
        for t in PRIORIDADE_TABELA:
            if t in tabs:
                escolha[linha] = t
                break
    h = h[h["linha"].map(escolha) == h["tipo_tabela"]].drop_duplicates(
        ["linha", "sentido", "tipo_dia", "horario_largada"])
    h = h[h["tipo_dia"].isin(SERVICOS)]

    linhas_novas = set(h["linha"])
    linhas_antigas = set(trips["linha"])
    sem_base = sorted(linhas_novas - linhas_antigas)
    sem_tabela = sorted(set(tabelas_por_linha.index) - set(escolha))
    log.append(f"Linhas com horário utilizável: {len(linhas_novas)}")
    log.append(f"  tabela usada: " + ", ".join(f"{t}={sum(1 for v in escolha.values() if v == t)}" for t in PRIORIDADE_TABELA))
    log.append(f"Linhas só com tabelas sazonais (ignoradas): {len(sem_tabela)} -> {' '.join(sem_tabela)}")
    log.append(f"Linhas sem viagem no GTFS antigo (sem traçado, ignoradas): {len(sem_base)} -> {' '.join(sem_base)}")

    # ---------- clonar viagem antiga mais próxima ----------
    grupos = {k: df for k, df in trips.groupby(["linha", "sentido"])}
    novos_trips, mapa_clone = [], []
    sem_sentido = set()
    for (linha, sentido, tipo_dia), hs in h.groupby(["linha", "sentido", "tipo_dia"]):
        base = grupos.get((linha, sentido))
        if base is None:
            if linha in linhas_antigas:
                sem_sentido.add(f"{linha}/{sentido}")
            continue
        mesmo_dia = base[base["tipo_dia"] == tipo_dia]
        if mesmo_dia.empty:
            mesmo_dia = base
        svc = SERVICOS[tipo_dia][0]
        route_id = base["route_id"].iloc[0]
        saidas_base = mesmo_dia["saida"].to_numpy()
        for _, r in hs.iterrows():
            saida = hms_para_seg(r["horario_largada"])
            # distância circular no dia, para 23:50 casar com 00:10
            d = abs(saidas_base - saida) % 86400
            d = pd.Series(d).where(d <= 43200, 86400 - d)
            modelo = mesmo_dia.iloc[int(d.to_numpy().argmin())]
            tid = f"{linha}-{sentido}-{svc}-{r['horario_largada'].replace(':', '')}"
            novos_trips.append({
                "route_id": route_id,
                "service_id": svc,
                "trip_id": tid,
                "trip_headsign": modelo["trip_headsign"],
                "direction_id": modelo["direction_id"],
                "shape_id": modelo["shape_id"],
                "wheelchair_accessible": "1" if r["adaptado_deficiente"].upper().startswith("S") else "2",
            })
            mapa_clone.append((tid, modelo["trip_id"], saida, int(modelo["duracao"])))
    if sem_sentido:
        log.append(f"Linha+sentido novos sem correspondente no GTFS antigo (ignorados): {len(sem_sentido)} -> {' '.join(sorted(sem_sentido))}")

    novos_trips = pd.DataFrame(novos_trips)
    clones = pd.DataFrame(mapa_clone, columns=["trip_id", "modelo", "saida", "duracao"])

    # stop_times: copia a sequência do modelo, horário só na 1ª e última parada
    modelos = st[st["trip_id"].isin(clones["modelo"])][["trip_id", "stop_id", "stop_sequence"]]
    modelos = modelos.rename(columns={"trip_id": "modelo"})
    ultimo = modelos.groupby("modelo")["stop_sequence"].max().rename("ult")
    novos_st = clones.merge(modelos, on="modelo").merge(ultimo, left_on="modelo", right_index=True)
    primeiro = novos_st.groupby("trip_id")["stop_sequence"].transform("min")
    hora = pd.Series("", index=novos_st.index)
    hora[novos_st["stop_sequence"] == primeiro] = novos_st["saida"].map(seg_para_hms)
    eh_ult = novos_st["stop_sequence"] == novos_st["ult"]
    hora[eh_ult] = (novos_st["saida"] + novos_st["duracao"])[eh_ult].map(seg_para_hms)
    novos_st["arrival_time"] = hora
    novos_st["departure_time"] = hora
    novos_st["timepoint"] = (hora != "").map({True: "1", False: "0"})
    novos_st = novos_st.sort_values(["trip_id", "stop_sequence"])[
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "timepoint"]]

    # ---------- calendário ----------
    calendar = pd.DataFrame([
        {"service_id": sid, **dict(zip(
            ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"], map(str, dias))),
         "start_date": INICIO.strftime("%Y%m%d"), "end_date": FIM.strftime("%Y%m%d")}
        for sid, dias in SERVICOS.values()
    ])
    cd = []
    for f in FERIADOS:
        dt = date.fromisoformat(f)
        regular = ["DU"] * 5 + ["SAB", "DOM"]
        cd.append({"service_id": regular[dt.weekday()], "date": dt.strftime("%Y%m%d"), "exception_type": "2"})
        cd.append({"service_id": "FER", "date": dt.strftime("%Y%m%d"), "exception_type": "1"})
    calendar_dates = pd.DataFrame(cd)

    # ---------- tabelas restantes ----------
    usados_route = set(novos_trips["route_id"])
    usados_shape = set(novos_trips["shape_id"])
    usados_stop = set(novos_st["stop_id"])
    out = {
        "agency": g["agency"],
        "routes": routes[routes["route_id"].isin(usados_route)],
        "stops": g["stops"][g["stops"]["stop_id"].isin(usados_stop)],
        "shapes": g["shapes"][g["shapes"]["shape_id"].isin(usados_shape)],
        "trips": novos_trips,
        "stop_times": novos_st,
        "calendar": calendar,
        "calendar_dates": calendar_dates,
        "feed_info": pd.DataFrame([{
            "feed_publisher_name": "projetoUberBus (derivado de dados EPTC)",
            "feed_publisher_url": "http://www.eptc.com.br",
            "feed_lang": "pt",
            "feed_start_date": INICIO.strftime("%Y%m%d"),
            "feed_end_date": FIM.strftime("%Y%m%d"),
            "feed_version": date.today().isoformat(),
        }]),
    }
    if "fare_attributes" in g:
        out["fare_attributes"] = g["fare_attributes"]

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(SAIDA, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, df in out.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            z.writestr(f"{nome}.txt", buf.getvalue())

    log.append("")
    log.append(f"GTFS gerado: {SAIDA}")
    for nome, df in out.items():
        log.append(f"  {nome}.txt: {len(df)} linhas")
    log.append(f"  linhas (routes) no feed: {len(out['routes'])}")
    RELATORIO.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    main()
