# Build + release do stack `acoes` no k3s do homelab.
#
# Uso:
#   make db-init   cria role/database `daytrade` no TimescaleDB compartilhado
#   make release   build, importa no containerd, carimba as tags no repo
#                  homelab e publica — o ArgoCD sincroniza sozinho
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
# A migration do schema NÃO roda aqui: no homelab ela é um Job com
# `argocd.argoproj.io/hook: PreSync` e dispara sozinha a cada sync.

KUBECONFIG ?= $(CURDIR)/../../homelab/k3s/kubeconfig/homelab.yaml
export KUBECONFIG

HOMELAB_DIR := $(CURDIR)/../../homelab
ACOES_MANIFESTS := $(HOMELAB_DIR)/applications/acoes
ARGO_APP := acoes

DB_NAME ?= daytrade
DB_USER ?= daytrade

# Tag = sha curto do HEAD, mais `-dirty.<timestamp>` se a árvore tiver mudança
# não commitada. `latest` não serve: com tag fixa o manifest nunca muda, o Argo
# não vê diferença e o pod nunca é recriado.
tag_of = $(shell s=$$(git rev-parse --short HEAD) && \
	[ -z "$$(git status --porcelain)" ] || s="$$s-dirty.$$(date +%Y%m%d%H%M%S)"; echo $$s)

BACKEND_TAG ?= $(call tag_of)
STREAMLIT_TAG ?= $(call tag_of)
BACKEND_IMAGE := acoes-backend:$(BACKEND_TAG)
STREAMLIT_IMAGE := acoes-streamlit:$(STREAMLIT_TAG)

# ⚠️ As tags são repassadas explicitamente a cada sub-make, e isso é
# OBRIGATÓRIO. Cada `$(MAKE)` reparseia este arquivo, o que reexecuta o
# `$(shell ...)` de `tag_of` — e o `date +%Y%m%d%H%M%S` do sufixo `-dirty`
# devolveria um valor NOVO. Como `import-all` leva minutos, o `manifests`
# seguinte gravaria uma tag que nunca foi buildada nem importada, e o pod
# subiria em ErrImageNeverPull. Variável passada na linha de comando vence a
# atribuição do arquivo, então isso congela a tag pro release inteiro.
RELEASE_TAGS = BACKEND_TAG='$(BACKEND_TAG)' STREAMLIT_TAG='$(STREAMLIT_TAG)'

.PHONY: help build-backend build-streamlit import-all db-init manifests publish sync release

help:
	@echo "make db-init   cria role/database '$(DB_NAME)' no timescaledb compartilhado"
	@echo "make release   build -> import -> manifests -> publish (Argo sincroniza sozinho)"
	@echo "make sync      força o sync agora, sem esperar o poll de 3min do Argo"
	@echo
	@echo "tags deste build:  $(BACKEND_IMAGE)  $(STREAMLIT_IMAGE)"

build-backend:
	docker build -t "$(BACKEND_IMAGE)" backend/

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
