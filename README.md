# Mega-Sena Discord Bot (Ubuntu VPS + Hostinger)

Bot de Discord focado na Mega-Sena, hospedável em Ubuntu. Ele:
- Consulta o feed oficial (`/home/ultimos-resultados`) e extrai **megasena**.
- Gera **10 jogos** com critérios anti-popularidade + diversidade (wheel reduzido).
- Avalia automaticamente os acertos dos 10 jogos quando sai o resultado.
- Publica 3 mensagens: resultado, recomendação para o próximo concurso, e lembrete no dia.

## 1) Requisitos

Ubuntu 22.04+ (Hostinger VPS). Instale:
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

## 2) Deploy rápido

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edite .env com seu token e caminho do data.xlsx
```

Coloque `data.xlsx` (histórico da Mega-Sena) na raiz do projeto, ou ajuste `DATA_XLSX_PATH` no `.env`.

## 3) Executar

```bash
source .venv/bin/activate
python bot.py
```

## 4) Rodar como serviço (systemd)

Crie `/etc/systemd/system/megabot.service`:

```
[Unit]
Description=Mega-Sena Discord Bot
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/caminho/para/mega-bot
Environment="PYTHONUNBUFFERED=1"
ExecStart=/caminho/para/mega-bot/.venv/bin/python /caminho/para/mega-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Então:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now megabot.service
sudo systemctl status megabot.service
journalctl -u megabot -f
```

## 5) Como usar no Discord

- `!programar` ou `!programar 123456789012345678` – define o canal para os envios automáticos.
- `!surpresinha` – gera 10 jogos recomendados imediatamente (e salva para avaliação).
- `!cancelar` – interrompe os envios automáticos neste servidor.
- `!help` – mostra ajuda.

## 6) Como funciona a lógica

- **Fonte**: `https://servicebus2.caixa.gov.br/portaldeloterias/api/home/ultimos-resultados`
- Quando detecta **novo** `numeroDoConcurso` **com dezenas**, envia:
  1. Resultado + avaliação dos 10 jogos salvos para esse concurso.
  2. Gera e publica 10 jogos para o **próximo** concurso e salva em `state/bets_{N+1}.json`.
  3. No dia do próximo sorteio (`dataProximoConcurso`), envia lembrete.

- **Seleção dos 10 jogos**:
  - Filtros: soma [170–215], ≥3 números >31, 2–4 ímpares, máx. 4 mesmo decênio, máx. 2 mesmo final, máx. 3 múltiplos de 5, sem sequências (≥3).
  - Busca gulosa priorizando: cobertura de pares/trincas, baixa sobreposição, limite de exposição por número.
  - Semente: `MEGASENA-{concurso}-{dataProximoConcurso}`.

## 7) Arquivos

- `bot.py`, `generator.py`, `config.py`, `requirements.txt`, `.env.example`, `README.md`
- `state/` – persistência de canal e apostas.
- `data.xlsx` – histórico opcional (parser tolerante).
