# acoes-scraper (VM Windows do homelab)

Roda continuamente na mesma VM Windows onde o terminal MetaTrader 5 está
aberto e logado, buscando candles a cada poucos segundos e enviando pro
`processor` via HTTP. Não guarda nenhuma credencial de banco — só conhece a
URL do processor.

É o único componente do pipeline que **não** roda no k3s: precisa do MT5,
que é DLL de Windows.

## Pré-requisito de rede (leia antes de configurar)

A VM tem duas placas de rede, e o scraper depende da segunda:

- **NIC1**, macvtap em modo bridge — dá o IP da LAN e serve pro acesso via
  área de trabalho remota.
- **NIC2**, na rede `default` do libvirt, `192.168.122.50/24` — é por onde o
  scraper alcança o cluster.

O motivo é que macvtap em modo bridge **isola o guest do próprio host que o
hospeda**: a VM enxerga o roteador e o resto da LAN, mas não o
`192.168.1.183`. Sem a NIC2, todo POST daria timeout. Ver a seção "Rede da VM
Windows" em `../docs/homelab-pipeline.md`.

Como o `processor` não tem registro DNS público de propósito, quem resolve o
nome é o arquivo hosts da VM. Adicione, como Administrador, em
`C:\Windows\System32\drivers\etc\hosts`:

```
192.168.122.1  acoes-processor.dondon.services
```

O certificado é Let's Encrypt válido pra `*.dondon.services` e a conexão é
feita pelo nome, então o TLS valida normalmente — não use `verify=False` nem
`-k` em lugar nenhum.

## Instalação

Na VM Windows, dentro do checkout do repositório:

```
pip install -r requirements.txt
pip install -r requirements-local.txt
pip install -r scraper/requirements.txt
```

## Configuração

Variáveis de ambiente (ver `scraper/config.py` para todos os valores padrão):

- `PROCESSOR_URL` — `https://acoes-processor.dondon.services` no homelab.
- `ACOES_API_KEY` — a mesma do Secret `acoes-db` no cluster. Sem ela o
  processor recusa os POSTs com 401.
- `POLL_INTERVAL_SECONDS` — padrão `5`.
- `SCRAPER_TIMEFRAMES` — padrão `M15,H1,H4,D1`.

## Rodar manualmente (teste)

```
set PROCESSOR_URL=https://acoes-processor.dondon.services
set ACOES_API_KEY=<a chave>
python scraper\scraper.py
```

Confirme que os dados estão chegando, pela API de leitura:

```
curl -s https://acoes-api.dondon.services/status
```

`last_ingested_at` avançando a cada poucos segundos = pipeline vivo.

## Rodando como serviço Windows (NSSM)

Task Scheduler é pra jobs disparados/periódicos — não serve bem pra "rodar
pra sempre, reiniciar se cair". [NSSM](https://nssm.cc/) registra o processo
como um serviço Windows de verdade, com start automático no boot e restart
automático em falha:

```
nssm install AcoesScraper "C:\caminho\pro\venv\Scripts\python.exe" "C:\caminho\pro\repo\scraper\scraper.py"
nssm set AcoesScraper AppDirectory "C:\caminho\pro\repo"
nssm set AcoesScraper AppEnvironmentExtra PROCESSOR_URL=https://acoes-processor.dondon.services ACOES_API_KEY=<a chave>
nssm set AcoesScraper AppExit Default Restart
nssm start AcoesScraper
```

Confirme que o serviço sobrevive a um reboot da VM antes de considerar a
Fase 2 concluída.
