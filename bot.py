import asyncio, json, os, datetime as dt
from typing import Dict, Any, List, Optional
import aiohttp
import discord
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo

from config import load_settings
from generator import GameGenerator, load_history_numbers_from_excel

SETTINGS = load_settings()
TZ = ZoneInfo(SETTINGS.timezone)

DATA_DIR = "./state"
os.makedirs(DATA_DIR, exist_ok=True)

STATE_PATH = os.path.join(DATA_DIR, "state.json")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MegaBot/1.0; +https://github.com)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://loterias.caixa.gov.br/",
    "Cache-Control": "no-cache",
}
HOME_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/home/ultimos-resultados"
MODALIDADE_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/megasena"

def log(msg: str):
    print(f"{dt.datetime.now(TZ).isoformat(timespec='seconds')}: {msg}")

def load_state()->Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"channels": {}, "last_processed_concurso": None, "reminder_sent_for": []}

def save_state(st: Dict[str, Any]):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def brl(v):
    if v is None:
        return "R$ 0,00"
    s = f"{float(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")

def parse_date_br(d: str)->dt.date:
    return dt.datetime.strptime(d, "%d/%m/%Y").date()

async def _fetch_json(session: aiohttp.ClientSession, url: str, retries: int = 3) -> Dict[str, Any]:
    last_status = None
    for attempt in range(retries):
        try:
            async with session.get(url, headers=DEFAULT_HEADERS, timeout=30) as resp:
                last_status = resp.status
                if resp.status == 200:
                    # alguns servidores retornam content-type estranho; ignora content_type
                    return await resp.json(content_type=None)
                await asyncio.sleep(1.5 * (attempt + 1))
        except Exception as e:
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Falha ao acessar {url} (status={last_status})")

async def fetch_megasena(session: aiohttp.ClientSession)->Dict[str, Any]:
    """
    Tenta a home/ultimos-resultados e cai para /megasena se falhar.
    Normaliza o dicionário de saída para o mesmo formato usado no resto do bot.
    """
    # 1) tenta a HOME
    try:
        home = await _fetch_json(session, HOME_URL)
        ms = home.get("megasena") or home.get("megaSena") or {}
        if ms:
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
    except Exception as e:
        log(f"[WARN] HOME falhou: {e}")

    # 2) fallback: /megasena (último concurso da modalidade)
    ms2 = await _fetch_json(session, MODALIDADE_URL)
    dezenas = [int(x) for x in (ms2.get("listaDezenas") or ms2.get("dezenas") or [])]
    return {
        "acumulado": bool(ms2.get("acumulado")),
        "dataApuracao": ms2.get("dataApuracao") or ms2.get("dataApuracaoStr"),
        "dataProximoConcurso": ms2.get("dataProximoConcurso"),
        "dezenas": dezenas,
        "numeroDoConcurso": int(ms2.get("numeroDoConcurso")) if ms2.get("numeroDoConcurso") is not None else None,
        "quantidadeGanhadores": ms2.get("quantidadeGanhadores") or ms2.get("quantidadeGanhadoresSena") or 0,
        "valorEstimadoProximoConcurso": float(ms2.get("valorEstimadoProximoConcurso") or 0.0),
        "valorPremio": float(ms2.get("valorPremio") or 0.0),
    }

def load_history_df():
    path = SETTINGS.data_xlsx_path
    if os.path.exists(path):
        try:
            return load_history_numbers_from_excel(path)
        except Exception as e:
            log(f"[WARN] Falha lendo {path}: {e}")
    return None

HISTORY_DF = load_history_df()

def generate_bets_for_concurso(concurso:int, seed_suffix:str="")->List[List[int]]:
    seed = f"MEGASENA-{concurso}-{seed_suffix}"
    gen = GameGenerator(seed=seed)
    return gen.generate(n_games=10)

def bets_path(concurso:int)->str:
    return os.path.join(DATA_DIR, f"bets_{concurso}.json")

def save_bets(concurso:int, bets:List[List[int]]):
    with open(bets_path(concurso), "w", encoding="utf-8") as f:
        json.dump({"concurso": concurso, "bets": bets}, f, ensure_ascii=False, indent=2)

def load_bets(concurso:int)->Optional[List[List[int]]]:
    p = bets_path(concurso)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f).get("bets")
    return None

def eval_hits(drawn: List[int], bets: List[List[int]]):
    drawn_set = set(drawn)
    hits_per_game = [len(drawn_set & set(b)) for b in bets]
    max_hits = max(hits_per_game) if hits_per_game else 0
    best_index = hits_per_game.index(max_hits) + 1 if hits_per_game else None
    return hits_per_game, best_index, max_hits

def fmt_games(bets: List[List[int]])->str:
    lines = []
    for i,b in enumerate(bets, 1):
        nums = " - ".join(f"{n:02d}" for n in sorted(b))
        lines.append(f"- Jogo {i}: {nums}")
    return "\n".join(lines)

def fmt_resultados_message(ms: Dict[str, Any], bets: Optional[List[List[int]]]):
    concurso = ms["numeroDoConcurso"]
    data_apur = ms["dataApuracao"]
    dezenas = ms["dezenas"]
    qtd_ganh = ms["quantidadeGanhadores"]
    valor_premio = ms["valorPremio"]
    prox_valor = ms["valorEstimadoProximoConcurso"]

    header = f"Esses foram os resultados do concurso **{concurso}** - **{data_apur}**"
    resultado = "Resultado: " + " - ".join(f"{d:02d}" for d in dezenas)

    if qtd_ganh and int(qtd_ganh) > 0:
        premio_line = f"Valor do prêmio (sena): {brl(valor_premio)}"
    else:
        premio_line = f"**Acumulou**. Valor estimado próximo: {brl(prox_valor)}"

    if bets:
        hits, best_idx, best = eval_hits(dezenas, bets)
        jogos_feitos = f"Quantos jogos foram feitos: {len(bets)}"
        acertos_list = "\n".join([f"- Jogo {i+1}: {h} acertos" for i,h in enumerate(hits)])
        melhor = f"Esse foi o jogo com mais assertividade: Jogo {best_idx} - acertos: {best}"
        body = f"{resultado}\n{jogos_feitos}\nQuantos números foram acertados em cada jogo:\n{acertos_list}\n{melhor}"
    else:
        body = resultado

    return f"{header}\n{premio_line}\n{body}"

def fmt_proximo_message(ms: Dict[str, Any], bets: List[List[int]]):
    concurso_atual = ms["numeroDoConcurso"]
    proximo_concurso = concurso_atual + 1 if concurso_atual is not None else None
    data_prox = ms["dataProximoConcurso"]
    valor = ms["valorEstimadoProximoConcurso"]
    header = f"Para o próximo concurso **{proximo_concurso}** - **{data_prox}**"
    valor_line = f"Valor estimado para o próximo concurso: {brl(valor)}"
    jogos = "Esses são os jogos recomendados:\n" + fmt_games(bets)
    return f"{header}\n{valor_line}\n{jogos}"

def fmt_lembrete_dia(concurso:int, valor:float):
    hoje = dt.datetime.now(TZ).strftime("%d/%m/%Y")
    return f"Hoje é o último dia para apostar no concurso **{concurso}** ({hoje}) com o valor estimado de {brl(valor)}"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@bot.event
async def on_ready():
    log(f"Logado como {bot.user} (ID {bot.user.id})")
    check_feed_loop.start()

# Mostra erro de comando no chat
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    await ctx.reply(f"⚠️ Erro ao executar comando: `{error}`")

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
        return  # não derruba a task

    if not ms.get("numeroDoConcurso"):
        return

    concurso = ms["numeroDoConcurso"]
    data_prox = ms.get("dataProximoConcurso")
    valor_prox = ms.get("valorEstimadoProximoConcurso", 0.0)

    # resultado novo disponível
    if st.get("last_processed_concurso") != concurso and ms.get("dezenas"):
        prior_bets = load_bets(concurso)
        msg1 = fmt_resultados_message(ms, prior_bets)

        prox_concurso = concurso + 1
        bets_prox = generate_bets_for_concurso(prox_concurso, seed_suffix=data_prox or "")
        save_bets(prox_concurso, bets_prox)
        msg2 = fmt_proximo_message(ms, bets_prox)

        for guild_id, channel_id in channels.items():
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

    # lembrete no dia do sorteio
    try:
        if data_prox:
            prox_date = parse_date_br(data_prox)
            today = dt.datetime.now(TZ).date()
            prox_concurso = concurso + 1
            if today == prox_date and prox_concurso not in st.get("reminder_sent_for", []):
                msg3 = fmt_lembrete_dia(prox_concurso, valor_prox)
                for guild_id, channel_id in channels.items():
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
    """Gera 10 jogos do próximo concurso. Se a API falhar, usa fallback local."""
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
    """Mostra o próximo concurso: número, data e valor estimado."""
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
            "⚠️ Não consegui consultar o feed agora.\n"
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
    )
    await ctx.reply(txt)

def main():
    bot.run(SETTINGS.token)

if __name__ == "__main__":
    main()
