# Coletor MT5 (VM Windows do homelab)

Roda continuamente na mesma VM Windows onde o terminal MetaTrader 5 está
aberto e logado, buscando candles a cada poucos segundos e enviando pro
processor via HTTP. Não guarda nenhuma credencial de banco — só conhece a
URL do processor.

## Instalação

Na VM Windows, dentro do checkout do repositório:

```
pip install -r requirements.txt
pip install -r requirements-local.txt
pip install -r collector/requirements.txt
```

## Configuração

Variáveis de ambiente (ver `collector/config.py` para todos os valores
padrão):

- `PROCESSOR_URL` — ex: `http://100.x.x.x:8000` (endereço Tailscale do
  processor no homelab).
- `PROCESSOR_API_KEY` — opcional, só se o processor exigir `X-API-Key`.
- `POLL_INTERVAL_SECONDS` — padrão `5`.
- `COLLECTOR_TIMEFRAMES` — padrão `M15,H1,H4,D1`.

## Rodar manualmente (teste)

```
set PROCESSOR_URL=http://100.x.x.x:8000
python collector\collector.py
```

Confirme no banco (via `psql` ou qualquer cliente Postgres) que
`SELECT * FROM candles ORDER BY ingested_at DESC LIMIT 20;` está
atualizando a cada poucos segundos.

## Rodando como serviço Windows (NSSM)

Task Scheduler é pra jobs disparados/periódicos — não serve bem pra "rodar
pra sempre, reiniciar se cair". [NSSM](https://nssm.cc/) registra o
processo como um serviço Windows de verdade, com start automático no boot
e restart automático em falha:

```
nssm install DayTradeCollector "C:\caminho\pro\venv\Scripts\python.exe" "C:\caminho\pro\repo\collector\collector.py"
nssm set DayTradeCollector AppDirectory "C:\caminho\pro\repo"
nssm set DayTradeCollector AppEnvironmentExtra PROCESSOR_URL=http://100.x.x.x:8000
nssm set DayTradeCollector AppExit Default Restart
nssm start DayTradeCollector
```

Confirme que o serviço sobrevive a um reboot da VM antes de considerar a
Fase 2 concluída.
