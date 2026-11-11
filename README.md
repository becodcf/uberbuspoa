# UberBus POA — MVP

Planejador de viagens de ônibus para Porto Alegre usando só dados abertos e software gratuito.

```
dados/ (EPTC)  ──►  etl/gerar_gtfs.py  ──►  otp/gtfs-poa.zip  ──►  OpenTripPlanner (:8080)
                                                                        ▲
                       navegador  ──►  backend/app.py (FastAPI :8000) ──┘
                                              └──► Nominatim (geocoding OSM)
```

## Rodar

Pré-requisitos: Python 3.11+ e Java 21+.

```powershell
.\iniciar.ps1            # sobe OTP + backend; abra http://localhost:8000
.\iniciar.ps1 -Rebuild   # depois de atualizar os CSVs em dados\
```

O OTP leva cerca de 30 s para carregar. `GET /api/saude` mostra se ele está no ar.

Na primeira execução o script baixa automaticamente os arquivos que não vão para o git:
- `otp.jar`: https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.9.0/otp-shaded-2.9.0.jar
- `poa.osm.pbf`: https://download.bbbike.org/osm/bbbike/PortoAlegre/PortoAlegre.osm.pbf

## API

| Rota | Descrição |
|---|---|
| `GET /api/geocode?q=Rua Ramiro Barcelos 2350` | endereço → lat/lon (Nominatim, máx. 1 req/s) |
| `GET /api/rota?de_lat=&de_lon=&para_lat=&para_lon=&quando=2026-11-11T08:00:00-03:00` | até 3 opções de viagem com trechos, horários e geometria (polyline) |
| `GET /api/saude` | status do OTP |

## Como o GTFS é gerado

- **Paradas, traçados e sequência de paradas**: GTFS EPTC de 18/09/2025.
- **Horários de saída**: `tabela_horaria_onibus.csv` (04/11/2026), tabela OFICIAL ou, na falta dela, EVENTUAL.
- Cada saída nova clona a viagem antiga mais próxima (mesma linha/sentido/tipo de dia) e herda a duração dela. Durações inválidas do GTFS antigo (chegada = saída) são trocadas pela mediana.
- Os horários intermediários são interpolados pelo OTP.
- Serviços: dia útil, sábado, domingo e feriado (a lista de feriados está em `FERIADOS`). Validade: 01/11/2026 a 31/10/2027.

O resumo de cada execução fica em `etl/relatorio.txt`.

## Limitações conhecidas

1. **157 linhas ficam de fora** porque não têm viagem/traçado no GTFS antigo: linhas novas, renumeradas, alimentadoras (A*, AE*), lotações etc. A lista está no relatório. Próximo passo: reconstruir o traçado a partir da sequência de ruas do `itinerario_onibus.csv` + OSM.
2. Itinerários que mudaram desde 09/2025 continuam com o traçado antigo.
3. Tabelas sazonais (VERÃO, CARNAVAL, PASSE LIVRE…) não são usadas.
4. Não há tempo real (GPS), só a tabela horária.
