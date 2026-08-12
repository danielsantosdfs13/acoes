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

## Como o código chega aqui

O scraper é o **único** componente do stack entregue por push. Todo o resto vai
pro k3s por imagem + ArgoCD; aqui não há container (o MetaTrader5 é DLL de
Windows), então o `Makefile` da raiz copia os arquivos por `scp` e reinicia o
serviço:

```bash
make release-scraper    # copia o codigo e reinicia o servico
make scraper-status     # versao implantada + estado do servico
make scraper-deps       # pip install remoto (lento, so quando requirements mudam)
```

O diretório de deploy é **`C:\acoes`**, com layout de dois níveis:

```
C:\acoes\daytrade_smc.py
C:\acoes\requirements.txt
C:\acoes\requirements-local.txt
C:\acoes\DEPLOY-INFO          <- gravado pelo make, tem VERSION e REQS_HASH
C:\acoes\scraper\scraper.py
C:\acoes\scraper\config.py
C:\acoes\scraper\requirements.txt
```

Os dois níveis são obrigatórios: `scraper.py` faz
`sys.path.insert(0, parent.parent)` pra achar `daytrade_smc`. Copiar só a pasta
`scraper/` quebra o import.

O `release-scraper` **para o serviço antes de copiar**, de propósito: o `scp`
grava arquivo a arquivo, e um `scraper.py` novo importando um `daytrade_smc.py`
velho pode explodir no meio do loop. A parada custa nada — o scraper reenvia as
últimas `TRAILING_WINDOW` velas a cada ciclo, então a lacuna se fecha sozinha no
primeiro loop depois de voltar.

## Bootstrap (uma vez só)

Tudo abaixo é manual e roda **uma vez**. Depois disso, o dia a dia é só
`make release-scraper`.

### 1. Habilitar o OpenSSH Server

PowerShell como Administrador, na VM:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
Get-NetFirewallRule -Name *OpenSSH-Server*   # a capability já cria a regra
```

**Não troque o shell padrão do sshd para PowerShell.** Os alvos `scraper-*`
usam sintaxe de `cmd` (`&` como separador, `if not exist`, `type`), que quebra
no PowerShell com erro difícil de diagnosticar. O `make scraper-check` detecta
isso e avisa, mas o mais simples é deixar como vem de fábrica. Se alguém já
tiver mudado:

```powershell
Remove-ItemProperty "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell
```

### 2. Instalar a chave pública

⚠️ **É aqui que quase todo mundo tropeça.** Se o usuário estiver no grupo
`Administrators`, o sshd do Windows **ignora** `~/.ssh/authorized_keys` e lê
apenas `C:\ProgramData\ssh\administrators_authorized_keys` — que ainda precisa
de ACL restrita. Qualquer permissão além de `Administrators` e `SYSTEM` faz o
sshd recusar a chave **em silêncio**, e o sintoma é só um
`Permission denied (publickey)` sem explicação nenhuma.

Descobrir qual caminho vale:

```powershell
(New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

Se **True**:

```powershell
$k = "C:\ProgramData\ssh\administrators_authorized_keys"
Add-Content $k "<conteúdo de ~/ssh-winvm/id_ed25519.pub do host>"
icacls $k /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

Se **False**: `C:\Users\<user>\.ssh\authorized_keys`, permissão só pro dono.
Nesse caso o `nssm restart` pode exigir elevação e falhar com `Access is
denied` — a saída é dar ao usuário permissão no serviço, ou usar conta
administradora.

### 3. Dependências

```
pip install -r C:\acoes\requirements.txt
pip install -r C:\acoes\requirements-local.txt
pip install -r C:\acoes\scraper\requirements.txt
```

Depois do bootstrap isso vira `make scraper-deps`, rodado do host.

### 4. Primeiro deploy e registro do serviço

Nesta ordem, porque o `scraper-check` exige o serviço registrado e o serviço
aponta pra arquivos que ainda não estão lá:

```bash
make scraper-files    # so copia, nao mexe em servico nenhum
make scraper-deps     # instala as dependencias na VM
```

Depois registre o NSSM (seção abaixo) e, daí em diante, o fluxo normal é
`make release-scraper`.

### Conta usada pelo SSH

Tem que ser uma conta **no grupo Administradores** — só para membros desse
grupo o sshd lê o `administrators_authorized_keys`. Nesta VM é a `admin`
(`VM_USER` no `Makefile`). Confira com:

```powershell
whoami
net localgroup Administradores
```

O `Administrador` embutido não serve: vem desabilitado de fábrica, e o sintoma
é a conexão ser **resetada antes** de negociar autenticação — diferente do
`Permission denied` que uma conta válida sem chave produz.

Se der `Permission denied`, o Event Log diz o motivo exato:

```powershell
Get-WinEvent -LogName 'OpenSSH/Operational' -MaxEvents 15 | Format-List TimeCreated,Message
```

`Invalid user X` significa que a conta não existe — não é problema de chave.

## Configuração

Variáveis de ambiente (ver `scraper/config.py` para todos os valores padrão):

- `PROCESSOR_URL` — `https://acoes-processor.dondon.services` no homelab.
- `ACOES_API_KEY` — a mesma do Secret `acoes-db` no cluster. Sem ela o
  processor recusa os POSTs com 401.
- `POLL_INTERVAL_SECONDS` — padrão `5`.
- `SCRAPER_TIMEFRAMES` — padrão `M15,H1,H4,D1`.

## Rodar manualmente (teste)

```
cd C:\acoes
set PROCESSOR_URL=https://acoes-processor.dondon.services
set ACOES_API_KEY=<a chave>
python scraper\scraper.py
```

A primeira linha do log traz a versão lida do `DEPLOY-INFO`, então dá pra
confirmar de olho que o que está rodando é o que você acabou de publicar.

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
nssm install AcoesScraper "C:\Python312\python.exe" "C:\acoes\scraper\scraper.py"
nssm set AcoesScraper AppDirectory "C:\acoes"
nssm set AcoesScraper AppEnvironmentExtra PROCESSOR_URL=https://acoes-processor.dondon.services ACOES_API_KEY=<a chave>
nssm set AcoesScraper AppExit Default Restart
nssm start AcoesScraper
```

Ajuste o caminho do `python.exe` pro seu (ou pro do venv, se usar um). Os
demais caminhos precisam bater com `VM_APP_DIR` no `Makefile` da raiz, que é
`C:\acoes`.

O `nssm` precisa estar no `PATH` — o `make scraper-check` chama `nssm status`
por SSH e falha com mensagem clara se não achar.

A `ACOES_API_KEY` fica **só aqui**, na config do serviço; o `Makefile` do host
nunca a lê nem a transporta. Para consultá-la:

```bash
kubectl -n acoes get secret acoes-db -o jsonpath='{.data.ACOES_API_KEY}' | base64 -d
```

Confirme que o serviço sobrevive a um reboot da VM antes de considerar a
Fase 2 concluída.

## Escolhendo o terminal e a conta do MT5

Por padrão o scraper anexa no terminal que estiver rodando, com a conta que
estiver logada. Isso basta enquanto existe **uma** conta na máquina — e vira
armadilha assim que existem duas (uma real e uma demo, por exemplo): nada
decide qual delas alimenta o pipeline, e o dado de uma entra no banco com o
mesmo nome de símbolo da outra.

Quatro variáveis opcionais resolvem, e você só precisa das duas primeiras:

| Variável | Para quê |
|---|---|
| `MT5_PATH` | qual `terminal64.exe` usar — o seletor principal |
| `MT5_LOGIN` | conta esperada; a coleta **falha** se o terminal servir outra |
| `MT5_SERVER` | servidor (ex.: `ClearInvestimentos-CLEAR`) |
| `MT5_PASSWORD` | só com os dois acima: faz o terminal **trocar** de conta |

`MT5_LOGIN` sozinho não loga nada — ele afirma. É a guarda barata: configure
mesmo que você só tenha uma conta, porque o serviço passa a parar com
mensagem clara em vez de coletar calado do lugar errado. Sem ele, o scraper
avisa no log a cada subida que aceita qualquer conta.

Ao subir, o serviço registra de onde o dado vem:

```
[INFO] Conta MT5: login=1000324286 servidor=ClearInvestimentos-CLEAR (Clear (XP
       Investimentos CCTVM)) tipo=REAL moeda=BRL | terminal=C:\Program Files\...
```

### Duas contas ao mesmo tempo (real + demo)

Instâncias abertas a partir da **mesma pasta** compartilham o diretório de
dados (`%APPDATA%\MetaQuotes\Terminal\<hash>`, derivado do caminho de
instalação) e brigam pela mesma configuração. Para separar de verdade, use
duas instalações — ou copie a pasta do terminal e rode a cópia com
`/portable`, que põe os dados ao lado do `.exe`:

```
nssm set AcoesScraper AppEnvironmentExtra ^
  PROCESSOR_URL=https://acoes-processor.dondon.services ^
  ACOES_API_KEY=<a chave> ^
  MT5_PATH="C:\Program Files\Clear Investimentos MT5 Terminal\terminal64.exe" ^
  MT5_LOGIN=1000324286
nssm restart AcoesScraper
```

Para apontar um processo na demo, troque as duas últimas pelo `.exe` da
segunda instalação e pelo login da conta demo. Repare que `AppEnvironmentExtra`
**substitui** a lista inteira — repita as variáveis que já estavam lá.
