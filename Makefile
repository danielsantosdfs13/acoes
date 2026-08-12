# Build + release do stack `acoes` no k3s do homelab.
#
# Uso:
#   make db-init          cria role/database `daytrade` no TimescaleDB compartilhado
#   make release          build, importa no containerd, carimba as tags no repo
#                         homelab e publica — o ArgoCD sincroniza sozinho
#   make release-scraper  copia o scraper pra VM Windows e reinicia o serviço
#
# Este host é o próprio node do k3s, então não há registry: imagens buildadas
# localmente vão direto pro containerd via `k3s ctr images import`, são
# referenciadas sem prefixo e com `imagePullPolicy: IfNotPresent`.
#
# ---------------------------------------------------------------------------
# ⚠️ ESTE MAKEFILE NÃO APLICA MANIFEST. Leia antes de "consertar" isso.
# ---------------------------------------------------------------------------
# A fonte de verdade dos manifests é o repo homelab, em `applications/acoes/`,
# sincronizada pelo ArgoCD (Application `acoes`). Não crie um alvo `apply` aqui.
#
# Não é preferência de estilo, é incidente medido no vizinho: em 2026-07-30 um
# `make release` do platform-fcar que fazia `kubectl apply` deixou OutOfSync
# exatamente os 18 recursos que ele tocava, e nenhum outro. `kubectl apply`
# client-side grava `kubectl.kubernetes.io/last-applied-configuration`, que não
# existe no estado desejado do Argo, e os dois competem pelo mesmo objeto.
# Ver o cabeçalho de platform-fcar/Makefile pro relato completo.
#
# O que mora aqui, e por quê:
#   - build de imagem + `k3s ctr images import` — não há registry, e GitOps
#     não builda imagem
#   - `db-init` — cria role/database, precisa de superusuário do Postgres
#   - `release-scraper` e os alvos `scraper-*` — o scraper roda na VM Windows
#     porque depende do MetaTrader5, que é DLL de Windows. Não há container,
#     logo não há imagem, logo o Argo não alcança. É o único componente
#     entregue por PUSH (ssh/scp) em vez de pull.
# A migration do schema NÃO roda aqui: no homelab ela é um Job com
# `argocd.argoproj.io/hook: PreSync` e dispara sozinha a cada sync.
#
# `release` e `release-scraper` são deliberadamente SEPARADOS — mesmo
# precedente de `release`/`release-vm` no platform-fcar. O deploy no cluster
# não pode falhar porque a VM Windows estava desligada.

KUBECONFIG ?= $(CURDIR)/../../homelab/k3s/kubeconfig/homelab.yaml
export KUBECONFIG

HOMELAB_DIR := $(CURDIR)/../../homelab
ACOES_MANIFESTS := $(HOMELAB_DIR)/applications/acoes
ARGO_APP := acoes

DB_NAME ?= daytrade
DB_USER ?= daytrade

# --- VM Windows do scraper (MT5) ---
# O IP é fixo por reserva DHCP na rede `default` do libvirt (MAC
# 52:54:00:c6:88:12 -> .50), não por config dentro do Windows. E é pela NIC2
# que ele responde: a NIC1 é macvtap, que isola o guest do próprio host.
VM_SSH_KEY ?= $(HOME)/ssh-winvm/id_ed25519
# `admin`, não `daniel`: é a conta que existe na VM e está no grupo
# Administradores — o que importa porque só para membros desse grupo o sshd lê
# C:\ProgramData\ssh\administrators_authorized_keys. Para os demais ele iria em
# C:\Users\<conta>\.ssh\authorized_keys e ignoraria a chave instalada lá.
VM_USER    ?= admin
VM_ADDR    ?= 192.168.122.50
VM_HOST    := $(VM_USER)@$(VM_ADDR)
VM_APP_DIR ?= C:/acoes
VM_SERVICE ?= AcoesScraper
# O executor é um SEGUNDO serviço na mesma VM, e a entrega tem que parar e
# subir os DOIS. Até 2026-08-12 só o scraper era reiniciado: os arquivos novos
# chegavam em C:\acoes\executor e o serviço seguia rodando o código velho em
# memória, indefinidamente. Falha silenciosa da pior espécie — o deploy diz
# que deu certo, o arquivo no disco é o novo, e o comportamento é o antigo.
VM_SERVICE_EXECUTOR ?= AcoesExecutor

# ConnectTimeout + BatchMode vêm do padrão de backup/scripts/backup-oracle-postgres.sh,
# não do Makefile do fcar: lá o ssh roda pelado e trava esperando quando o
# destino some. Com uma VM que pode estar desligada, travar é o pior caso.
SSH_VM = ssh -i $(VM_SSH_KEY) -o ConnectTimeout=10 -o BatchMode=yes $(VM_HOST)
SCP_VM = scp -i $(VM_SSH_KEY) -o ConnectTimeout=10 -o BatchMode=yes

# Fechamento do que o scraper precisa pra rodar. `daytrade_smc.py` não importa
# nenhum módulo de primeira parte (só stdlib + numpy/pandas), então a lista
# para aqui — backend/ e streamlit_app.py não têm nada a ver com a VM.
#
# O layout de DOIS NÍVEIS é obrigatório: scraper.py faz
# sys.path.insert(0, parent.parent) pra achar daytrade_smc. Copiar só a pasta
# scraper/ quebra o import.
# `execucao.py` viaja junto porque envio de ordem é DLL de Windows, igual à
# coleta: quem manda ordem tem que rodar aqui, não no k3s. Ele não é
# importado por `scraper.py` — vai para a VM porque o executor (e qualquer
# teste manual por SSH) precisa dele ao lado do motor.
SCRAPER_ROOT_FILES := daytrade_smc.py execucao.py
SCRAPER_SUB_FILES  := scraper/scraper.py scraper/config.py
# O executor vai pro mesmo lugar pelo mesmo motivo do scraper: envio de ordem
# é DLL de Windows. Ele é um SERVIÇO separado na VM (nssm), não um modo do
# scraper — o scraper não pode parar de coletar porque o envio de ordem
# quebrou, e vice-versa.
EXECUTOR_FILES     := executor/executor.py
SCRAPER_REQS       := requirements.txt requirements-local.txt scraper/requirements.txt

# Tag = sha curto do HEAD, mais `-dirty.<timestamp>` se a árvore tiver mudança
# não commitada. `latest` não serve: com tag fixa o manifest nunca muda, o Argo
# não vê diferença e o pod nunca é recriado.
tag_of = $(shell s=$$(git rev-parse --short HEAD) && \
	[ -z "$$(git status --porcelain)" ] || s="$$s-dirty.$$(date +%Y%m%d%H%M%S)"; echo $$s)

BACKEND_TAG ?= $(call tag_of)
STREAMLIT_TAG ?= $(call tag_of)
BACKEND_IMAGE := acoes-backend:$(BACKEND_TAG)
STREAMLIT_IMAGE := acoes-streamlit:$(STREAMLIT_TAG)

# O mesmo `tag_of` serve de versão do scraper — é o equivalente da tag de
# imagem do lado do k3s, só que gravada num arquivo na VM em vez de no
# manifest. Diferente de RELEASE_TAGS abaixo, aqui NÃO há corrida: o
# release-scraper calcula a versão uma vez e grava, sem encadear sub-makes.
SCRAPER_VERSION ?= $(call tag_of)
SCRAPER_REQS_HASH := $(shell cat $(SCRAPER_REQS) | md5sum | cut -c1-32)

# ⚠️ As tags são repassadas explicitamente a cada sub-make, e isso é
# OBRIGATÓRIO. Cada `$(MAKE)` reparseia este arquivo, o que reexecuta o
# `$(shell ...)` de `tag_of` — e o `date +%Y%m%d%H%M%S` do sufixo `-dirty`
# devolveria um valor NOVO. Como `import-all` leva minutos, o `manifests`
# seguinte gravaria uma tag que nunca foi buildada nem importada, e o pod
# subiria em ErrImageNeverPull. Variável passada na linha de comando vence a
# atribuição do arquivo, então isso congela a tag pro release inteiro.
RELEASE_TAGS = BACKEND_TAG='$(BACKEND_TAG)' STREAMLIT_TAG='$(STREAMLIT_TAG)'

.PHONY: help build-backend build-streamlit import-all db-init manifests publish sync release \
        scraper-check scraper-files scraper-push scraper-deps scraper-status release-scraper

help:
	@echo "k3s (ArgoCD):"
	@echo "  make db-init          cria role/database '$(DB_NAME)' no timescaledb compartilhado"
	@echo "  make release          build -> import -> manifests -> publish (Argo sincroniza sozinho)"
	@echo "  make sync             força o sync agora, sem esperar o poll de 3min do Argo"
	@echo
	@echo "VM Windows (scraper, push por ssh):"
	@echo "  make release-scraper  copia o codigo e reinicia o servico $(VM_SERVICE)"
	@echo "  make scraper-files    so copia, sem mexer no servico (use no 1o deploy)"
	@echo "  make scraper-deps     pip install remoto (lento, so quando requirements mudam)"
	@echo "  make scraper-status   versao implantada + estado do servico"
	@echo
	@echo "tags deste build:  $(BACKEND_IMAGE)  $(STREAMLIT_IMAGE)"
	@echo "scraper:           $(SCRAPER_VERSION) -> $(VM_HOST):$(VM_APP_DIR)"

# Contexto na RAIZ (e não em backend/) porque o analyzer.py roda o motor, e
# `daytrade_smc.py` mora aqui fora. Mesmo formato do build do streamlit logo
# abaixo. Ver o cabeçalho do backend/Dockerfile e o .dockerignore.
build-backend:
	docker build -f backend/Dockerfile -t "$(BACKEND_IMAGE)" .

build-streamlit:
	docker build -f Dockerfile.streamlit -t "$(STREAMLIT_IMAGE)" .

import-all: build-backend build-streamlit
	docker save "$(BACKEND_IMAGE)" "$(STREAMLIT_IMAGE)" | sudo k3s ctr images import -

# Precisa de superusuário, por isso roda por `kubectl exec` no pod e não por
# GitOps. Idempotente: pode rodar quantas vezes quiser.
db-init:
	@test -n "$(DB_PASSWORD)" || { echo "ERRO: passe DB_PASSWORD=... (o mesmo do Secret acoes-db)"; exit 1; }
	./scripts/init-tenant-db.sh "$(DB_NAME)" "$(DB_USER)" "$(DB_PASSWORD)"

manifests:
	@for img in "$(BACKEND_IMAGE)" "$(STREAMLIT_IMAGE)"; do \
		sudo k3s ctr images ls -q | grep -qx "docker.io/library/$$img" || { \
			echo "ERRO: $$img nao esta no containerd - rode 'make import-all' antes."; \
			echo "      (rodar 'make manifests' solto recomputa o timestamp da tag suja"; \
			echo "       e nunca vai casar com o que foi importado - use 'make release')"; \
			exit 1; }; \
	done
	@echo ">> gravando tags em $(ACOES_MANIFESTS)"
	@sed -i -E 's|^(\s*image: )acoes-backend:.*|\1$(BACKEND_IMAGE)|' $(ACOES_MANIFESTS)/*.yaml
	@sed -i -E 's|^(\s*image: )acoes-streamlit:.*|\1$(STREAMLIT_IMAGE)|' $(ACOES_MANIFESTS)/*.yaml
	@grep -nE '^\s*image: acoes-' $(ACOES_MANIFESTS)/*.yaml
	@! grep -qE '^\s*image: acoes-(backend|streamlit):latest' $(ACOES_MANIFESTS)/*.yaml || { \
		echo "ERRO: sobrou acoes-*:latest em algum manifest (grep acima) - a tag nao foi carimbada."; \
		exit 1; }

publish:
	@cd $(HOMELAB_DIR) && \
	if git diff --quiet -- applications/acoes/; then \
		echo ">> nenhuma mudanca de manifest (mesmo sha, arvore limpa) - nada a publicar"; \
	else \
		git add applications/acoes/ && \
		git commit -q -m "acoes: backend $(BACKEND_TAG), streamlit $(STREAMLIT_TAG)" && \
		git push -q origin main && \
		echo ">> publicado $$(git rev-parse --short HEAD)"; \
	fi

# A Application `acoes` tem syncPolicy automated, então o Argo pega sozinho em
# até 3 min. Este alvo só existe pra não esperar — e passa a revisão exata
# recém-publicada, evitando sincronizar contra o commit anterior.
sync:
	@cd $(HOMELAB_DIR) && REV=$$(git rev-parse HEAD) && \
	echo ">> sync $(ARGO_APP) @ $$REV" && \
	kubectl -n argocd patch app $(ARGO_APP) --type merge \
	  -p "{\"operation\":{\"sync\":{\"revision\":\"$$REV\",\"syncStrategy\":{\"hook\":{}}},\"initiatedBy\":{\"username\":\"make-release\"}}}" >/dev/null && \
	for i in $$(seq 1 100); do \
		P=$$(kubectl -n argocd get app $(ARGO_APP) -o jsonpath='{.status.operationState.phase}'); \
		case "$$P" in Succeeded) echo ">> sync OK"; exit 0;; Failed|Error) \
			echo ">> sync $$P:"; \
			kubectl -n argocd get app $(ARGO_APP) -o jsonpath='{.status.operationState.message}{"\n"}'; \
			exit 1;; esac; \
		sleep 3; \
	done; echo ">> timeout esperando o sync"; exit 1

release:
	$(MAKE) import-all $(RELEASE_TAGS)
	$(MAKE) manifests $(RELEASE_TAGS)
	$(MAKE) publish $(RELEASE_TAGS)
	@echo ">> release publicado. O Argo sincroniza em ate 3min, ou rode 'make sync'."

# ===========================================================================
# Scraper na VM Windows — entrega por PUSH (ssh/scp), fora do ArgoCD
# ===========================================================================

# Guardas antes de qualquer coisa destrutiva. Cada falha diz o que fazer, em
# vez de deixar o ssh/scp cuspir erro cru mais adiante.
scraper-check:
	@test -f "$(VM_SSH_KEY)" || { \
		echo "ERRO: chave $(VM_SSH_KEY) nao existe."; \
		echo "      gere com: ssh-keygen -t ed25519 -f $(VM_SSH_KEY) -C acoes-scraper -N ''"; \
		exit 1; }
	@$(SSH_VM) "exit" 2>/dev/null || { \
		printf '%s\n' \
		  "ERRO: nao consegui abrir ssh em $(VM_HOST)." \
		  "      confira: VM ligada, OpenSSH Server habilitado, e a chave publica" \
		  "      instalada. Se o usuario for admin, ela vai em" \
		  '      C:\ProgramData\ssh\administrators_authorized_keys (ACL so' \
		  "      Administrators+SYSTEM) - o ~/.ssh/authorized_keys e IGNORADO." \
		  "      ver scraper/README.md"; \
		exit 1; }
	@$(SSH_VM) "echo %COMSPEC%" 2>/dev/null | grep -qi 'cmd.exe' || { \
		printf '%s\n' \
		  "ERRO: o shell padrao do sshd na VM nao e o cmd.exe." \
		  "      Os alvos scraper-* usam sintaxe de cmd ('&', 'if not exist', 'type');" \
		  "      com PowerShell como shell padrao eles quebram com erro incompreensivel." \
		  "      Volte ao padrao removendo a chave DefaultShell:" \
		  '        Remove-ItemProperty "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell'; \
		exit 1; }
	@$(SSH_VM) "nssm status $(VM_SERVICE)" >/dev/null 2>&1 || { \
		echo "ERRO: servico $(VM_SERVICE) nao existe na VM (ou nssm nao esta no PATH)."; \
		echo "      registre uma vez seguindo scraper/README.md"; \
		exit 1; }
	@echo ">> VM $(VM_HOST) ok, shell cmd, servico $(VM_SERVICE) registrado"

# Para o servico antes de copiar: o scp grava arquivo a arquivo, e um
# scraper.py novo importando um daytrade_smc.py velho pode explodir no meio do
# loop. A parada custa nada - o scraper reenvia as ultimas TRAILING_WINDOW
# velas a cada ciclo (ver scraper/scraper.py), entao a lacuna se fecha sozinha
# no primeiro loop depois de voltar. E a mesma propriedade que ja cobre
# reinicio de VM e loop perdido.
# Só a cópia, sem tocar no serviço. Existe pro primeiro deploy: o NSSM precisa
# apontar pra um scraper.py que ainda não está lá, e o `scraper-check` exige o
# serviço registrado — sem este alvo, os dois ficariam se esperando.
#   1. make scraper-files      <- arquivos chegam em C:\acoes
#   2. registra o NSSM na VM   <- ver scraper/README.md
#   3. make release-scraper    <- daqui em diante, o fluxo normal
scraper-files:
	@$(SSH_VM) "if not exist $(subst /,\\,$(VM_APP_DIR))\\scraper mkdir $(subst /,\\,$(VM_APP_DIR))\\scraper"
	@echo ">> copiando $(SCRAPER_ROOT_FILES) $(SCRAPER_SUB_FILES) $(SCRAPER_REQS)"
	@$(SCP_VM) $(SCRAPER_ROOT_FILES) requirements.txt requirements-local.txt $(VM_HOST):$(VM_APP_DIR)/
	@$(SCP_VM) $(SCRAPER_SUB_FILES) scraper/requirements.txt $(VM_HOST):$(VM_APP_DIR)/scraper/
	@$(SSH_VM) "if not exist $(subst /,\\,$(VM_APP_DIR))\\executor mkdir $(subst /,\\,$(VM_APP_DIR))\\executor"
	@$(SCP_VM) $(EXECUTOR_FILES) $(VM_HOST):$(VM_APP_DIR)/executor/
	@$(SSH_VM) "(echo VERSION=$(SCRAPER_VERSION)& echo REQS_HASH=$(SCRAPER_REQS_HASH)& echo DEPLOYED_AT=$$(date -Is)) > $(subst /,\\,$(VM_APP_DIR))\\DEPLOY-INFO"
	@echo ">> arquivos em $(VM_HOST):$(VM_APP_DIR) (versao $(SCRAPER_VERSION))"

scraper-push:
	@echo ">> parando $(VM_SERVICE) e $(VM_SERVICE_EXECUTOR)"
	@$(SSH_VM) "nssm stop $(VM_SERVICE)" >/dev/null 2>&1 || true
	@$(SSH_VM) "nssm stop $(VM_SERVICE_EXECUTOR)" >/dev/null 2>&1 || true
	@$(MAKE) --no-print-directory scraper-files SCRAPER_VERSION='$(SCRAPER_VERSION)'
	@echo ">> subindo $(VM_SERVICE)"
	@$(SSH_VM) "nssm start $(VM_SERVICE)" >/dev/null 2>&1 || { \
		echo "ERRO: nao consegui iniciar $(VM_SERVICE). Rode 'make scraper-status'."; \
		exit 1; }
	@# O executor pode nao estar registrado (VM nova, ou quem so coleta), entao
	@# falhar aqui nao derruba a entrega do scraper - mas TEM que aparecer, ou
	@# o codigo novo fica no disco sem nunca rodar.
	@echo ">> subindo $(VM_SERVICE_EXECUTOR)"
	@$(SSH_VM) "nssm start $(VM_SERVICE_EXECUTOR)" >/dev/null 2>&1 || \
		echo "   AVISO: $(VM_SERVICE_EXECUTOR) nao subiu (nao registrado?). Confira com 'make scraper-status'."
	@echo ">> scraper $(SCRAPER_VERSION) em $(VM_HOST):$(VM_APP_DIR)"

# Lento (pip resolve tudo), por isso fica fora do release-scraper. Rode quando
# o release avisar que os requirements mudaram.
scraper-deps:
	@echo ">> instalando dependencias na VM (pode demorar)"
	@$(SSH_VM) "cd /d $(subst /,\\,$(VM_APP_DIR)) && python -m pip install -r requirements.txt -r requirements-local.txt -r scraper\\requirements.txt"
	@$(SSH_VM) "(echo VERSION=$(SCRAPER_VERSION)& echo REQS_HASH=$(SCRAPER_REQS_HASH)& echo DEPLOYED_AT=$$(date -Is)) > $(subst /,\\,$(VM_APP_DIR))\\DEPLOY-INFO"
	@echo ">> dependencias em dia (hash $(SCRAPER_REQS_HASH))"

scraper-status:
	@echo "=== servicos ==="
	@printf '  %-16s ' "$(VM_SERVICE)"; $(SSH_VM) "nssm status $(VM_SERVICE)" 2>&1 || true
	@printf '  %-16s ' "$(VM_SERVICE_EXECUTOR)"; $(SSH_VM) "nssm status $(VM_SERVICE_EXECUTOR)" 2>&1 || true
	@echo "=== versao implantada ==="
	@$(SSH_VM) "type $(subst /,\\,$(VM_APP_DIR))\\DEPLOY-INFO" 2>&1 || echo "  (sem DEPLOY-INFO - nunca teve deploy)"
	@echo "=== versao aqui ==="
	@echo "  VERSION=$(SCRAPER_VERSION)"
	@echo "  REQS_HASH=$(SCRAPER_REQS_HASH)"

release-scraper:
	@$(MAKE) --no-print-directory scraper-check
	@REMOTE=$$($(SSH_VM) "type $(subst /,\\,$(VM_APP_DIR))\\DEPLOY-INFO" 2>/dev/null | tr -d '\r' | sed -n 's/^REQS_HASH=//p'); \
	$(MAKE) --no-print-directory scraper-push SCRAPER_VERSION='$(SCRAPER_VERSION)'; \
	if [ -n "$$REMOTE" ] && [ "$$REMOTE" != "$(SCRAPER_REQS_HASH)" ]; then \
		echo ">> AVISO: os requirements mudaram desde o ultimo deploy"; \
		echo "          ($$REMOTE -> $(SCRAPER_REQS_HASH)). Rode 'make scraper-deps'."; \
	fi
