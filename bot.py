# bot.py
# -*- coding: utf-8 -*-
"""
Bot de Discord focado na Mega-Sena.

Fontes de dados (ordem de tentativa):
  1) API comunitária estável: https://loteriascaixa-api.herokuapp.com/api/megasena/latest
  2) API oficial HOME:       https://servicebus2.caixa.gov.br/portaldeloterias/api/home/ultimos-resultados
  3) API oficial modalidade: https://servicebus2.caixa.gov.br/portaldeloterias/api/megasena
  4) Scraping do site oficial com BeautifulSoup (sem proxy)
  5) Cache local do último JSON válido (até 36h)

Fluxo automático:
1) Resultado do concurso encerrado + avaliação dos jogos salvos.
2) Jogos sugeridos para o próximo concurso.
3) Lembrete no dia do próximo sorteio.

Comandos:
!programar [id_do_canal]
!cancelar
!surpresinha [novos] [balanced] [n=10] [shuffle=0|1] [perfil=alto|misto|historico] [min_high=2]
             [sum_target=...] [sum_weight=...] [bucket_weight=...]
!proximo-jogo
!help
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import datetime as dt
from typing import Dict, Any, List, Optional, Tuple
import random as _random

import aiohttp
import discord
from discord.ext import commands, tasks
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from config import load_settings
from generator import GameGenerator, load_history_numbers_from_excel

# -----------------------------------------------------------------------------
# Configuração e caminhos
# -----------------------------------------------------------------------------

SETTINGS = load_settings()
TZ = ZoneInfo(SETTINGS.timezone)

DATA_DIR = "./state"
os.makedirs(DATA_DIR, exist_ok=True)

STATE_PATH = os.path.join(DATA_DIR, "state.json")

# Cache do último JSON válido (para operar mesmo sob 403/timeouts)
LAST_GOOD_PATH = os.path.join(DATA_DIR, "last_megasena.json")
MAX_STALE_HOURS = 36  # aceita cache "stale" por até 36h

# Cabeçalhos "de navegador" ajudam a reduzir 403 em alguns WAFs
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://loterias.caixa.gov.br/",
    "Origin": "https://loterias.caixa.gov.br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
_env_cookie = os.environ.get("CAIXA_COOKIE")
if _env_cookie:
    DEFAULT_HEADERS["Cookie"] = _env_cookie

# Endpoints
ALT_API_URL = "https://loteriascaixa-api.herokuapp.com/api/megasena/latest"
HOME_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/home/ultimos-resultados"
MODALIDADE_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/megasena"
SCRAPE_URL = "https://loterias.caixa.gov.br/wps/portal/loterias/landing/megasena"

# -----------------------------------------------------------------------------
# Utilidades de log/estado/cache
# -----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"{dt.datetime.now(TZ).isoformat(timespec='seconds')}: {msg}")

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"channels": {}, "last_processed_concurso": None, "reminder_sent_for": []}

def save_state(st: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def _save_last_good(ms: Dict[str, Any]) -> None:
    payload = {"saved_at": dt.datetime.now(TZ).isoformat(), "megasena": ms}
    with open(LAST_GOOD_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _load_last_good() -> Optional[Dict[str, Any]]:
    if not os.path.exists(LAST_GOOD_PATH):
        return None
    try:
        with open(LAST_GOOD_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        saved_at = dt.datetime.fromisoformat(payload.get("saved_at"))
        age = (dt.datetime.now(TZ) - saved_at).total_seconds()
        if age <= MAX_STALE_HOURS * 3600:
            return payload.get("megasena")
    except Exception:
        pass
    return None

# -----------------------------------------------------------------------------
# Formatação e parsing
# -----------------------------------------------------------------------------

def brl(v) -> str:
    if v is None:
        return "R$ 0,00"
    s = f"{float(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")

def parse_date_br(d: str) -> dt.date:
    return dt.datetime.strptime(d, "%d/%m/%Y").date()

# -----------------------------------------------------------------------------
# HTTP com retry/backoff e normalização de respostas
# -----------------------------------------------------------------------------

async def _fetch_json(session: aiohttp.ClientSession, url: str, retries: int = 3, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    last_status = None
    hdrs = headers or DEFAULT_HEADERS
    for attempt in range(retries):
        try:
            async with session.get(url, headers=hdrs, timeout=30) as resp:
                last_status = resp.status
                if resp.status == 200:
                    return await resp.json(content_type=None)
                await asyncio.sleep(1.0 + 0.5 * attempt + _random.random())
        except Exception:
            await asyncio.sleep(1.0 + 0.5 * attempt + _random.random())
    raise RuntimeError(f"Falha ao acessar {url} (status={last_status})")

def _normalize_home(ms: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "acumulado": ms.get("acumulado", False),
        "dataApuracao": ms.get("dataApuracao"),
        "dataProximoConcurso": ms.get("dataProximoConcurso"),
        "dezenas": [int(x) for x in (ms.get("dezenas") or [])],
        "numeroDoConcurso": int(ms.get("numeroDoConcurso")) if ms.get("numeroDoConcurso") is not None else None,
        "quantidadeGanhadores": ms.get("quantidadeGanhadores", 0),
        "valorEstimadoProximoConcurso": float(ms.get("valorEstimadoProximoConcurso") or 0.0),
        "valorPremio": float(ms.get("valorPremio") or 0.0),
    }

def _normalize_modalidade(ms2: Dict[str, Any]) -> Dict[str, Any]:
    dezenas = [int(x) for x in (ms2.get("listaDezenas") or ms2.get("dezenas") or [])]
    return {
        "acumulado": bool(ms2.get("acumulado")),
        "dataApuracao": ms2.get("dataApuracao") or ms2.get("dataApuracaoStr"),
        "dataProximoConcurso": ms2.get("dataProximoConcurso"),
        "dezenas": sorted(dezenas) if dezenas else [],
        "numeroDoConcurso": int(ms2.get("numeroDoConcurso")) if ms2.get("numeroDoConcurso") is not None else None,
        "quantidadeGanhadores": ms2.get("quantidadeGanhadores") or ms2.get("quantidadeGanhadoresSena") or 0,
        "valorEstimadoProximoConcurso": float(ms2.get("valorEstimadoProximoConcurso") or 0.0),
        "valorPremio": float(ms2.get("valorPremio") or 0.0),
    }

def _normalize_alt_api(js: Dict[str, Any]) -> Dict[str, Any]:
    dezenas_strs = js.get("dezenas") or []
    dezenas = [int(x) for x in dezenas_strs if str(x).isdigit()]
    dezenas = sorted(dezenas)
    concurso = js.get("concurso")
    data = js.get("data")  # dd/mm/aaaa
    prox_data = js.get("dataProximoConcurso")
    acumulou = bool(js.get("acumulou", False))
    valor_estimado = float(js.get("valorEstimadoProximoConcurso") or 0.0)

    qtd_ganh = 0
    valor_premio = 0.0
    for pr in js.get("premiacoes", []):
        if pr.get("faixa") == 1:
            try:
                qtd_ganh = int(pr.get("ganhadores") or 0)
            except Exception:
                qtd_ganh = 0
            try:
                valor_premio = float(pr.get("valorPremio") or 0.0)
            except Exception:
                valor_premio = 0.0
            break

    out = {
        "acumulado": acumulou,
        "dataApuracao": data,
        "dataProximoConcurso": prox_data,
        "dezenas": dezenas,
        "numeroDoConcurso": int(concurso) if concurso is not None else None,
        "quantidadeGanhadores": qtd_ganh,
        "valorEstimadoProximoConcurso": valor_estimado,
        "valorPremio": valor_premio,
    }
    return out

# -----------------------------------------------------------------------------
# SCRAPING (fallback) — BeautifulSoup no site oficial
# -----------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+")
_CONCURSO_RE = re.compile(r"concurso\s*([\d\.]+)", re.I)
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_CURRENCY_RE = re.compile(r"R\$\s*[\d\.\,]+")

def _to_int_safe(s: str) -> Optional[int]:
    try:
        return int(s)
    except Exception:
        try:
            return int(s.replace(".", ""))  # 2.906 -> 2906
        except Exception:
            return None

def _parse_currency_to_float(text: str) -> Optional[float]:
    m = _CURRENCY_RE.search(text or "")
    if not m:
        return None
    val = m.group(0)
    val = val.replace("R$", "").strip()
    val = val.replace(".", "").replace(",", ".")
    try:
        return float(val)
    except Exception:
        return None

async def fetch_via_scrape(session: aiohttp.ClientSession) -> Dict[str, Any]:
    hdrs = DEFAULT_HEADERS.copy()
    hdrs["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    async with session.get(SCRAPE_URL, headers=hdrs, timeout=30, allow_redirects=True) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Scrape falhou (status={resp.status})")
        html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    dezenas: List[int] = []
    ul_candidates = []
    ul_candidates += soup.select("ul.numbers.megasena")
    ul_candidates += soup.select("ul.numbers.mega-sena")
    ul_candidates += soup.select("ul.numbers.mega")
    if not ul_candidates:
        for ul in soup.find_all("ul"):
            lis = ul.find_all("li")
            if 6 <= len(lis) <= 15 and all(_NUM_RE.search(li.get_text(strip=True) or "") for li in lis):
                ul_candidates.append(ul)
                break
    if ul_candidates:
        lis = ul_candidates[0].find_all("li")
        for li in lis:
            numtxt = (li.get_text(strip=True) or "").strip()
            if numtxt.isdigit():
                dezenas.append(int(numtxt))
        dezenas = sorted(set(dezenas))[:6]

    text_all = soup.get_text(separator=" ", strip=True)
    concurso: Optional[int] = None
    m = _CONCURSO_RE.search(text_all)
    if m:
        concurso = _to_int_safe(m.group(1))

    data_apuracao: Optional[str] = None
    mdate = _DATE_RE.search(text_all)
    if mdate:
        data_apuracao = mdate.group(1)

    valor_prox: Optional[float] = None
    prox_data: Optional[str] = None
    estim_nodes = [el for el in soup.find_all(text=re.compile("estimad", re.I))]
    if estim_nodes:
        for node in estim_nodes:
            ctx_text = " ".join([node.strip(), node.parent.get_text(" ", strip=True) if node.parent else ""])
            money = _parse_currency_to_float(ctx_text)
            if money:
                valor_prox = money
                mdate2 = _DATE_RE.search(ctx_text)
                if mdate2:
                    prox_data = mdate2.group(1)
                break

    out = {
        "acumulado": False,
        "dataApuracao": data_apuracao,
        "dataProximoConcurso": prox_data,
        "dezenas": dezenas,
        "numeroDoConcurso": concurso,
        "quantidadeGanhadores": 0,
        "valorEstimadoProximoConcurso": float(valor_prox or 0.0),
        "valorPremio": 0.0,
    }
    return out

# -----------------------------------------------------------------------------
# Função principal de obtenção resiliente (com NOVA API como 1ª opção)
# -----------------------------------------------------------------------------

async def fetch_megasena(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """
    Ordem de tentativa:
      1) API Heroku (comunitária) /latest
      2) API HOME (oficial)
      3) API MODALIDADE (oficial)
      4) Scraping do site oficial
      5) Cache local
    """
    try:
        js = await _fetch_json(session, ALT_API_URL, headers={"Accept": "application/json", "User-Agent": DEFAULT_HEADERS["User-Agent"]})
        out = _normalize_alt_api(js)
        if out.get("numeroDoConcurso") or out.get("dezenas"):
            _save_last_good(out)
        return out
    except Exception as e:
        log(f"[WARN] ALT_API falhou: {e}")

    try:
        home = await _fetch_json(session, HOME_URL)
        ms = home.get("megasena") or home.get("megaSena") or {}
        if ms:
            out = _normalize_home(ms)
            _save_last_good(out)
            return out
    except Exception as e:
        log(f"[WARN] HOME falhou: {e}")

    try:
        ms2 = await _fetch_json(session, MODALIDADE_URL)
        out = _normalize_modalidade(ms2)
        _save_last_good(out)
        return out
    except Exception as e:
        log(f"[WARN] MODALIDADE falhou: {e}")

    try:
        out = await fetch_via_scrape(session)
        if (out.get("numeroDoConcurso") is not None) or out.get("dezenas"):
            _save_last_good(out)
        return out
    except Exception as e:
        log(f"[WARN] SCRAPE falhou: {e}")

    cached = _load_last_good()
    if cached:
        log("[INFO] usando cache local (stale)")
        return cached

    raise RuntimeError("Sem acesso às fontes e sem cache local disponível")

# -----------------------------------------------------------------------------
# Dados históricos (opcional) e geração de jogos
# -----------------------------------------------------------------------------

def load_history_df():
    path = SETTINGS.data_xlsx_path
    if os.path.exists(path):
        try:
            return load_history_numbers_from_excel(path)
        except Exception as e:
            log(f"[WARN] Falha lendo {path}: {e}")
    return None

HISTORY_DF = load_history_df()

def generate_bets_for_concurso(
    concurso: int,
    seed_suffix: str = "",
    *,
    n_games: int = 10,
    balanced: bool = False,
    shuffle: bool = True,
    novos: bool = False,
    # --- novos knobs para dialogar com generator.py ---
    profile: str = None,
    min_high: Optional[int] = None,
    sum_target: Optional[float] = None,
    sum_weight: Optional[float] = None,
    bucket_weight: Optional[float] = None,
) -> List[List[int]]:
    """
    Gera apostas para um concurso.
    - n_games: quantidade de jogos
    - balanced: tenta cobertura uniforme (quando possível)
    - shuffle: embaralha a ordem de apresentação das dezenas
    - novos: se True, usa um 'sal' temporal aleatório para não repetir jogos
    - profile/min_high/sum_target/sum_weight/bucket_weight: passam direto ao GameGenerator
    """
    salt = ""
    if novos:
        # sal temporal + aleatório para mudar a semente e gerar jogos diferentes
        salt = f"-NEW-{dt.datetime.now(TZ).isoformat()}-{_random.randint(0, 1_000_000)}"
    seed = f"MEGASENA-{concurso}-{seed_suffix}{salt}"

    # perfil padrão por env (sem depender do config.py): SURPRESINHA_PROFILE
    env_profile = os.getenv("SURPRESINHA_PROFILE", "").strip().lower() or None
    effective_profile = (profile or env_profile or "historico").lower()

    gen = GameGenerator(
        seed=seed,
        display_shuffle=shuffle,
        profile=effective_profile,
        min_high=min_high if min_high is not None else 1,
        sum_target=sum_target,
        sum_weight=sum_weight,
        bucket_weight=bucket_weight if bucket_weight is not None else 0.20,
    )
    return gen.generate(n_games=n_games, balanced=balanced)

def bets_path(concurso: int) -> str:
    return os.path.join(DATA_DIR, f"bets_{concurso}.json")

def save_bets(concurso: int, bets: List[List[int]]) -> None:
    with open(bets_path(concurso), "w", encoding="utf-8") as f:
        json.dump({"concurso": concurso, "bets": bets}, f, ensure_ascii=False, indent=2)

def load_bets(concurso: int) -> Optional[List[List[int]]]:
    p = bets_path(concurso)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("bets")
    return None

# -----------------------------------------------------------------------------
# Avaliação e mensagens
# -----------------------------------------------------------------------------

def eval_hits(drawn: List[int], bets: List[List[int]]) -> Tuple[List[int], Optional[int], int]:
    drawn_set = set(drawn)
    hits_per_game = [len(drawn_set & set(b)) for b in bets]
    max_hits = max(hits_per_game) if hits_per_game else 0
    best_index = hits_per_game.index(max_hits) + 1 if hits_per_game else None
    return hits_per_game, best_index, max_hits

def fmt_games(bets: List[List[int]], *, sort_output: bool = False) -> str:
    """
    sort_output=False (padrão): mantém a ordem recebida, que já vem embaralhada
    pelo GameGenerator (display_shuffle=True), evitando a impressão de "começar baixo".
    """
    lines = []
    for i, b in enumerate(bets, 1):
        seq = sorted(b) if sort_output else b
        nums = " - ".join(f"{n:02d}" for n in seq)
        lines.append(f"- Jogo {i}: {nums}")
    return "\n".join(lines)

def fmt_resultados_message(ms: Dict[str, Any], bets: Optional[List[List[int]]]) -> str:
    concurso = ms.get("numeroDoConcurso")
    data_apur = ms.get("dataApuracao")
    dezenas = ms.get("dezenas") or []
    qtd_ganh = ms.get("quantidadeGanhadores", 0)
    valor_premio = ms.get("valorPremio", 0.0)
    prox_valor = ms.get("valorEstimadoProximoConcurso", 0.0)

    header = f"Esses foram os resultados do concurso **{concurso}** - **{data_apur}**"
    resultado = "Resultado: " + (" - ".join(f"{d:02d}" for d in dezenas) if dezenas else "indisponível")

    if qtd_ganh and int(qtd_ganh) > 0:
        premio_line = f"Valor do prêmio (sena): {brl(valor_premio)}"
    else:
        premio_line = f"**Acumulou**. Valor estimado próximo: {brl(prox_valor)}"

    if bets:
        if dezenas:
            hits, best_idx, best = eval_hits(dezenas, bets)
            acertos_list = "\n".join([f"- Jogo {i+1}: {h} acertos" for i, h in enumerate(hits)])
            melhor = f"Esse foi o jogo com mais assertividade: Jogo {best_idx} - acertos: {best}"
        else:
            acertos_list = "- Sem avaliação (resultado indisponível)"
            melhor = "Esse foi o jogo com mais assertividade: —"
        body = (
            f"{resultado}\n"
            f"Quantos jogos foram feitos: {len(bets)}\n"
            f"Quantos números foram acertados em cada jogo:\n{acertos_list}\n{melhor}"
        )
    else:
        body = resultado

    return f"{header}\n{premio_line}\n{body}"

def fmt_proximo_message(ms: Dict[str, Any], bets: List[List[int]]) -> str:
    concurso_atual = ms.get("numeroDoConcurso")
    proximo_concurso = (concurso_atual + 1) if isinstance(concurso_atual, int) else "—"
    data_prox = ms.get("dataProximoConcurso") or "Não informado"
    valor = ms.get("valorEstimadoProximoConcurso", 0.0)
    header = f"Para o próximo concurso **{proximo_concurso}** - **{data_prox}**"
    valor_line = f"Valor estimado para o próximo concurso: {brl(valor)}"
    jogos = "Esses são os jogos recomendados:\n" + fmt_games(bets)
    return f"{header}\n{valor_line}\n{jogos}"

def fmt_lembrete_dia(concurso: int, valor: float) -> str:
    hoje = dt.datetime.now(TZ).strftime("%d/%m/%Y")
    return f"Hoje é o último dia para apostar no concurso **{concurso}** ({hoje}) com o valor estimado de {brl(valor)}"

# -----------------------------------------------------------------------------
# Discord: intents, bot e eventos
# -----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # habilite Message Content Intent no Dev Portal

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@bot.event
async def on_ready():
    log(f"Logado como {bot.user} (ID {bot.user.id})")
    check_feed_loop.start()

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    try:
        await ctx.reply(f"⚠️ Erro ao executar comando: `{error}`")
    except Exception:
        log(f"[WARN] Falha ao enviar erro para o canal: {error}")

# -----------------------------------------------------------------------------
# Tarefa em background: verificação periódica
# -----------------------------------------------------------------------------

@tasks.loop(seconds=SETTINGS.check_interval_seconds)
async def check_feed_loop():
    st = load_state()
    channels = st.get("channels", {})
    if not channels:
        return

    try:
        async with aiohttp.ClientSession() as session:
            ms = await fetch_megasena(session)
    except Exception as e:
        log(f"[WARN] check_feed_loop: {e}")
        return

    if not ms.get("numeroDoConcurso"):
        return

    concurso = ms["numeroDoConcurso"]
    data_prox = ms.get("dataProximoConcurso")
    valor_prox = ms.get("valorEstimadoProximoConcurso", 0.0)

    # Resultado novo
    if st.get("last_processed_concurso") != concurso and ms.get("dezenas"):
        prior_bets = load_bets(concurso)
        msg1 = fmt_resultados_message(ms, prior_bets)

        prox_concurso = concurso + 1
        bets_prox = generate_bets_for_concurso(prox_concurso, seed_suffix=data_prox or "",
                                               n_games=SETTINGS.surpresinha_default_n,
                                               balanced=SETTINGS.surpresinha_default_balanced,
                                               shuffle=SETTINGS.surpresinha_default_shuffle,
                                               profile=os.getenv("SURPRESINHA_PROFILE", "historico"))
        save_bets(prox_concurso, bets_prox)
        msg2 = fmt_proximo_message(ms, bets_prox)

        for _, channel_id in channels.items():
            ch = bot.get_channel(int(channel_id))
            if ch:
                try:
                    await ch.send(msg1)
                    await ch.send(msg2)
                except Exception as e:
                    log(f"[WARN] Falha ao enviar em {channel_id}: {e}")

        st["last_processed_concurso"] = concurso
        if prox_concurso in st.get("reminder_sent_for", []):
            st.get("reminder_sent_for", []).remove(prox_concurso)
        save_state(st)

    # Lembrete no dia do sorteio
    try:
        if data_prox:
            prox_date = parse_date_br(data_prox)
            today = dt.datetime.now(TZ).date()
            prox_concurso = concurso + 1
            if today == prox_date and prox_concurso not in st.get("reminder_sent_for", []):
                msg3 = fmt_lembrete_dia(prox_concurso, valor_prox)
                for _, channel_id in channels.items():
                    ch = bot.get_channel(int(channel_id))
                    if ch:
                        try:
                            await ch.send(msg3)
                        except Exception as e:
                            log(f"[WARN] Falha ao enviar lembrete em {channel_id}: {e}")
                st.setdefault("reminder_sent_for", []).append(prox_concurso)
                save_state(st)
    except Exception as e:
        log(f"[WARN] lembrete: {e}")

# -----------------------------------------------------------------------------
# Helpers de parsing
# -----------------------------------------------------------------------------

def _parse_surpresinha_args(args: Tuple[str, ...]) -> Dict[str, Any]:
    """
    Suporta tokens:
      - 'novos' | '--novos'           -> gera nova semente (jogos diferentes)
      - 'balanced'                    -> modo balanceado
      - 'n=10' ou '--n' '10'          -> quantidade de jogos
      - 'shuffle=0|1'                 -> exibição embaralhada (1) ou ordenada (0)
      - 'perfil=alto|misto|historico' -> define perfil do gerador (atalhos: 'alto', 'misto', 'historico')
      - 'min_high=2'                  -> mínimo de dezenas na faixa 41–60
      - 'sum_target=...'              -> alvo de soma
      - 'sum_weight=...'              -> peso da penalidade de soma (0 desliga)
      - 'bucket_weight=...'           -> peso do desvio de buckets
    """
    cfg = {
        "novos": False,
        "balanced": SETTINGS.surpresinha_default_balanced,
        "n": SETTINGS.surpresinha_default_n,
        "shuffle": SETTINGS.surpresinha_default_shuffle,
        "profile": None,
        "min_high": None,
        "sum_target": None,
        "sum_weight": None,
        "bucket_weight": None,
    }
    args = list(args or [])

    def _as_float(x):
        try:
            return float(x)
        except Exception:
            return None

    def _as_int(x):
        try:
            return int(x)
        except Exception:
            return None

    i = 0
    while i < len(args):
        tok = str(args[i]).strip().lower()

        if tok in ("novos", "--novos", "novo"):
            cfg["novos"] = True

        elif tok in ("balanced", "--balanced"):
            cfg["balanced"] = True

        elif tok.startswith("n="):
            v = _as_int(tok.split("=", 1)[1])
            if v is not None and v > 0:
                cfg["n"] = v
        elif tok in ("n", "--n") and i + 1 < len(args):
            v = _as_int(args[i + 1])
            if v is not None and v > 0:
                cfg["n"] = v
                i += 1

        elif tok.startswith("shuffle="):
            v = tok.split("=", 1)[1]
            cfg["shuffle"] = (v not in ("0", "false", "no"))

        # perfil (com atalho)
        elif tok in ("alto", "misto", "historico"):
            cfg["profile"] = tok
        elif tok.startswith("perfil=") or tok.startswith("profile="):
            v = tok.split("=", 1)[1]
            v = v.strip().lower()
            if v in ("alto", "misto", "historico"):
                cfg["profile"] = v

        elif tok.startswith("min_high="):
            v = _as_int(tok.split("=", 1)[1])
            if v is not None and v >= 0:
                cfg["min_high"] = v

        elif tok.startswith("sum_target="):
            v = _as_float(tok.split("=", 1)[1])
            if v is not None:
                cfg["sum_target"] = v

        elif tok.startswith("sum_weight="):
            v = _as_float(tok.split("=", 1)[1])
            if v is not None and v >= 0:
                cfg["sum_weight"] = v

        elif tok.startswith("bucket_weight="):
            v = _as_float(tok.split("=", 1)[1])
            if v is not None and v >= 0:
                cfg["bucket_weight"] = v

        i += 1

    return cfg

# -----------------------------------------------------------------------------
# Comandos
# -----------------------------------------------------------------------------

@bot.command(name="programar")
async def programar(ctx: commands.Context, canal_id: Optional[int] = None):
    if canal_id is None:
        canal_id = ctx.channel.id
    st = load_state()
    st["channels"][str(ctx.guild.id)] = str(canal_id)
    save_state(st)
    await ctx.reply(f"✅ Programado! Mensagens serão enviadas em <#{canal_id}>.")

@bot.command(name="cancelar")
async def cancelar(ctx: commands.Context):
    st = load_state()
    if str(ctx.guild.id) in st.get("channels", {}):
        st["channels"].pop(str(ctx.guild.id), None)
        save_state(st)
        await ctx.reply("🛑 Cancelado! Este servidor não receberá mais mensagens automáticas.")
    else:
        await ctx.reply("Nada para cancelar aqui.")

@bot.command(name="surpresinha")
async def surpresinha(ctx: commands.Context, *args):
    """
    Gera jogos para o próximo concurso.

    Exemplos:
      !surpresinha
      !surpresinha novos
      !surpresinha balanced n=12
      !surpresinha perfil=alto min_high=3
      !surpresinha perfil=misto sum_target=183 sum_weight=0.1
      !surpresinha historico bucket_weight=0.15

    Observação: se nada for informado, usamos os defaults do config.py
    (SURPRESINHA_COUNT, SURPRESINHA_BALANCED, SURPRESINHA_SHUFFLE) e perfil 'historico'.
    """
    opts = _parse_surpresinha_args(args)

    ms = None
    api_err = None
    try:
        async with aiohttp.ClientSession() as session:
            ms = await fetch_megasena(session)
    except Exception as e:
        api_err = str(e)

    if ms and ms.get("numeroDoConcurso"):
        concurso_atual = ms["numeroDoConcurso"] or 0
        proximo = concurso_atual + 1
        seed_suffix = ms.get("dataProximoConcurso") or ""
    else:
        st = load_state()
        last = st.get("last_processed_concurso") or 0
        proximo = (last + 1) if last else 0
        seed_suffix = dt.datetime.now(TZ).strftime("%Y-%m-%d")

    bets = generate_bets_for_concurso(
        proximo,
        seed_suffix=seed_suffix,
        n_games=opts["n"],
        balanced=opts["balanced"],
        shuffle=opts["shuffle"],
        novos=opts["novos"],
        profile=opts["profile"],
        min_high=opts["min_high"],
        sum_target=opts["sum_target"],
        sum_weight=opts["sum_weight"],
        bucket_weight=opts["bucket_weight"],
    )
    save_bets(proximo, bets)

    # Header amigável com flags relevantes
    header = f"Esses são os jogos recomendados para o concurso **{proximo}**"
    flags = []
    if opts["novos"]:
        flags.append("novos")
    if opts["balanced"]:
        flags.append("balanceados")
    if opts["profile"]:
        flags.append(f"perfil={opts['profile']}")
    if opts["min_high"] is not None:
        flags.append(f"min_high={opts['min_high']}")
    if opts["n"] != SETTINGS.surpresinha_default_n:
        flags.append(f"{opts['n']} jogos")
    if flags:
        header += " (" + ", ".join(flags) + ")"
    header += ":\n"

    body = fmt_games(bets, sort_output=not opts["shuffle"])
    note = ""
    if api_err:
        note = (
            "\n\n_(Obs.: não consegui consultar as fontes agora; gerei via fallback local.)_ "
            f"`{api_err}`"
        )
    await ctx.reply(header + body + note)

@bot.command(name="proximo-jogo")
async def proximo_jogo(ctx: commands.Context):
    """
    Mostra o próximo concurso (número, data e prêmio estimado).
    Usa a API nova e demais fallbacks; se tudo falhar, mostra estimativa local.
    """
    try:
        async with aiohttp.ClientSession() as session:
            ms = await fetch_megasena(session)

        if ms.get("numeroDoConcurso") is not None:
            concurso_atual = ms["numeroDoConcurso"]
            proximo = concurso_atual + 1
            data_prox = ms.get("dataProximoConcurso") or "Não informado"
            valor_prox = ms.get("valorEstimadoProximoConcurso") or 0.0
            msg = (
                f"**Próximo concurso:** **{proximo}**\n"
                f"**Data do sorteio:** {data_prox}\n"
                f"**Prêmio estimado:** {brl(valor_prox)}\n"
                f"_Concurso atual (último apurado): {concurso_atual}_"
            )
            await ctx.reply(msg)
            return

        raise RuntimeError("Resposta sem numeroDoConcurso")

    except Exception as e:
        st = load_state()
        last = st.get("last_processed_concurso") or 0
        proximo = last + 1 if last else "desconhecido"
        msg = (
            "⚠️ Não consegui consultar as fontes agora.\n"
            f"**Próximo concurso (estimado):** **{proximo}**\n"
            "**Data do sorteio:** Indisponível\n"
            "**Prêmio estimado:** Indisponível\n"
            f"_Detalhe técnico: {e}_"
        )
        await ctx.reply(msg)

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    txt = (
        "**Comandos disponíveis**\n"
        "`!programar [id_do_canal]` – Define onde o bot enviará as mensagens.\n"
        "`!cancelar` – Cancela os envios automáticos neste servidor.\n"
        "`!surpresinha [novos] [balanced] [n=10] [shuffle=0|1] "
        "[perfil=alto|misto|historico] [min_high=2] [sum_target=...] [sum_weight=...] [bucket_weight=...]` – "
        "Gera jogos recomendados agora.\n"
        "`!proximo-jogo` – Mostra o número do próximo concurso, data e prêmio estimado.\n"
        "\n**Fluxo automático**\n"
        "• Mensagem 1: Resultado do concurso e avaliação dos jogos salvos.\n"
        "• Mensagem 2: Jogos para o próximo concurso.\n"
        "• Mensagem 3: Lembrete no dia do sorteio.\n"
        "\n**Observações técnicas**\n"
        "• Requer **Message Content Intent** habilitado no Developer Portal do Discord.\n"
        "• Ordem de fontes: API Heroku → APIs oficiais → scraping → cache local (36h).\n"
    )
    await ctx.reply(txt)

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    bot.run(SETTINGS.token)

if __name__ == "__main__":
    main()
