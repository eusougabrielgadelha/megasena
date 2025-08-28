# bot.py
# -*- coding: utf-8 -*-
"""
Bot de Discord focado na Mega-Sena.

Robustez contra 403:
- Tenta a API oficial (HOME).
- Fallback para a API da modalidade (/megasena).
- Fallback 2 (novo): SCRAPING do site oficial com BeautifulSoup (sem proxy).
- Fallback 3: cache local do último JSON válido (até 36h), para manter operação.

Fluxo automático:
1) Resultado do concurso encerrado + avaliação dos 10 jogos salvos.
2) 10 jogos sugeridos para o próximo concurso.
3) Lembrete no dia do próximo sorteio.

Comandos:
!programar [id_do_canal]
!cancelar
!surpresinha
!proximo-jogo
!help

Requisitos extras:
- beautifulsoup4 (instale: `pip install beautifulsoup4`)
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
# Opcional: permitir cookies customizados via variável de ambiente (se necessário)
# Ex.: export CAIXA_COOKIE="JSESSIONID=...; outra=..."
_env_cookie = os.environ.get("CAIXA_COOKIE")
if _env_cookie:
    DEFAULT_HEADERS["Cookie"] = _env_cookie

# Endpoints oficiais
HOME_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/home/ultimos-resultados"
MODALIDADE_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/megasena"

# Página de scraping oficial (HTTPS para evitar redirects de 302/HTTP)
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

async def _fetch_json(session: aiohttp.ClientSession, url: str, retries: int = 3) -> Dict[str, Any]:
    last_status = None
    for attempt in range(retries):
        try:
            async with session.get(url, headers=DEFAULT_HEADERS, timeout=30) as resp:
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

# -----------------------------------------------------------------------------
# SCRAPING (fallback 2) — BeautifulSoup no site oficial
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
    """
    Converte 'R$ 12.345.678,90' -> 12345678.90
    """
    m = _CURRENCY_RE.search(text or "")
    if not m:
        return None
    val = m.group(0)
    # remove R$, espaços, separador milhar ".", troca vírgula por ponto
    val = val.replace("R$", "").strip()
    val = val.replace(".", "").replace(",", ".")
    try:
        return float(val)
    except Exception:
        return None

async def fetch_via_scrape(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """
    Baixa o HTML da landing oficial e extrai:
    - dezenas (ul.numbers.megasena / ul.numbers.mega-sena / variações)
    - número do concurso (texto "Concurso 2906" etc.)
    - data de apuração (primeira data dd/mm/aaaa próxima do bloco de resultado)
    - (melhor esforço) valor estimado do próximo concurso (texto contendo 'estimad')
    """
    hdrs = DEFAULT_HEADERS.copy()
    # Para HTML, aceitar text/html ajuda a passar por alguns filtros
    hdrs["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    async with session.get(SCRAPE_URL, headers=hdrs, timeout=30, allow_redirects=True) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Scrape falhou (status={resp.status})")
        html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    # 1) dezenas — tente múltiplos seletores robustos
    dezenas: List[int] = []
    # Seletores comuns:
    # ul class="numbers megasena", "numbers mega-sena", "numbers mega", etc
    ul_candidates = []
    ul_candidates += soup.select("ul.numbers.megasena")
    ul_candidates += soup.select("ul.numbers.mega-sena")
    ul_candidates += soup.select("ul.numbers.mega")
    # fallback genérico: primeira UL com 6 LIs numéricos
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
        dezenas = sorted(set(dezenas))[:6]  # defensivo

    # 2) número do concurso — busca por "Concurso 2.906" etc
    concurso: Optional[int] = None
    text_all = soup.get_text(separator=" ", strip=True)
    m = _CONCURSO_RE.search(text_all)
    if m:
        concurso = _to_int_safe(m.group(1))

    # 3) data de apuração — primeira data dd/mm/aaaa perto do topo
    # (melhor esforço: usa a primeira data encontrada no documento)
    data_apuracao: Optional[str] = None
    mdate = _DATE_RE.search(text_all)
    if mdate:
        data_apuracao = mdate.group(1)

    # 4) valor estimado próximo — procurar bloco com 'estimad'
    valor_prox: Optional[float] = None
    prox_data: Optional[str] = None
    # tente localizar frases com "estimad" (estimado/estimativa), e extrair R$
    estim_nodes = [el for el in soup.find_all(text=re.compile("estimad", re.I))]
    if estim_nodes:
        # procura a moeda no texto do próprio nó ou em pais próximos
        for node in estim_nodes:
            ctx_text = " ".join([node.strip(), node.parent.get_text(" ", strip=True) if node.parent else ""])
            money = _parse_currency_to_float(ctx_text)
            if money:
                valor_prox = money
                # procura data dd/mm/aaaa no mesmo contexto
                mdate2 = _DATE_RE.search(ctx_text)
                if mdate2:
                    prox_data = mdate2.group(1)
                break

    # Resultado consolidado (com defaults para campos não extraídos)
    out = {
        "acumulado": False,  # desconhecido via HTML simples
        "dataApuracao": data_apuracao,
        "dataProximoConcurso": prox_data,
        "dezenas": dezenas,
        "numeroDoConcurso": concurso,
        "quantidadeGanhadores": 0,  # desconhecido aqui
        "valorEstimadoProximoConcurso": float(valor_prox or 0.0),
        "valorPremio": 0.0,  # desconhecido aqui
    }
    return out

# -----------------------------------------------------------------------------
# Função principal de obtenção resiliente
# -----------------------------------------------------------------------------

async def fetch_megasena(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """
    Ordem de tentativa:
      1) API HOME (/home/ultimos-resultados)
      2) API MODALIDADE (/megasena)
      3) SCRAPING do site oficial (BeautifulSoup)
      4) Cache local (stale)
    """
    # 1) HOME
    try:
        home = await _fetch_json(session, HOME_URL)
        ms = home.get("megasena") or home.get("megaSena") or {}
        if ms:
            out = _normalize_home(ms)
            _save_last_good(out)
            return out
    except Exception as e:
        log(f"[WARN] HOME falhou: {e}")

    # 2) MODALIDADE
    try:
        ms2 = await _fetch_json(session, MODALIDADE_URL)
        out = _normalize_modalidade(ms2)
        _save_last_good(out)
        return out
    except Exception as e:
        log(f"[WARN] MODALIDADE falhou: {e}")

    # 3) SCRAPING (sem proxy)
    try:
        out = await fetch_via_scrape(session)
        # Só salva se ao menos concurso ou dezenas foram obtidos (para não gravar lixo)
        if (out.get("numeroDoConcurso") is not None) or out.get("dezenas"):
            _save_last_good(out)
        return out
    except Exception as e:
        log(f"[WARN] SCRAPE falhou: {e}")

    # 4) CACHE LOCAL
    cached = _load_last_good()
    if cached:
        log("[INFO] usando cache local (stale)")
        return cached

    raise RuntimeError("Sem acesso aos endpoints e sem cache local disponível")

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

def generate_bets_for_concurso(concurso: int, seed_suffix: str = "") -> List[List[int]]:
    seed = f"MEGASENA-{concurso}-{seed_suffix}"
    gen = GameGenerator(seed=seed)
    return gen.generate(n_games=10)

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

def fmt_games(bets: List[List[int]]) -> str:
    lines = []
    for i, b in enumerate(bets, 1):
        nums = " - ".join(f"{n:02d}" for n in sorted(b))
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
        bets_prox = generate_bets_for_concurso(prox_concurso, seed_suffix=data_prox or "")
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
            st["reminder_sent_for"].remove(prox_concurso)
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
async def surpresinha(ctx: commands.Context):
    """
    Gera 10 jogos do próximo concurso.
    Se a API falhar, usa fallback local (último concurso processado + data atual como semente).
    """
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

    bets = generate_bets_for_concurso(proximo, seed_suffix=seed_suffix)
    save_bets(proximo, bets)

    header = f"Esses são os jogos recomendados para o concurso **{proximo}**:\n"
    body = fmt_games(bets)
    note = ""
    if api_err:
        note = (
            "\n\n_(Obs.: não consegui consultar o feed agora; gerei via fallback local.)_ "
            f"`{api_err}`"
        )
    await ctx.reply(header + body + note)

@bot.command(name="proximo-jogo")
async def proximo_jogo(ctx: commands.Context):
    """
    Mostra o próximo concurso (número, data e prêmio estimado).
    Usa endpoints oficiais e scraping; se tudo falhar, mostra estimativa local.
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
            "⚠️ Não consegui consultar o site/endpoint agora.\n"
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
        "`!surpresinha` – Gera 10 jogos recomendados agora.\n"
        "`!proximo-jogo` – Mostra o número do próximo concurso, data e prêmio estimado.\n"
        "\n**Fluxo automático**\n"
        "• Mensagem 1: Resultado do concurso e avaliação dos 10 jogos salvos.\n"
        "• Mensagem 2: 10 jogos para o próximo concurso.\n"
        "• Mensagem 3: Lembrete no dia do sorteio.\n"
        "\n**Observações técnicas**\n"
        "• Requer **Message Content Intent** habilitado no Developer Portal do Discord.\n"
        "• Para reduzir 403, usamos headers de navegador, scraping HTML e cache local de 36h.\n"
        "• Opcional: defina `CAIXA_COOKIE` no ambiente se precisar enviar cookies na requisição.\n"
    )
    await ctx.reply(txt)

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    bot.run(SETTINGS.token)

if __name__ == "__main__":
    main()
