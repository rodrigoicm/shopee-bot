# 🚀 Ecossistema de Automação de Ofertas (Shopee, Mercado Livre & WhatsApp)

Sistema completo em **Python** para automação de conversão de links de afiliados (**Shopee** e **Mercado Livre**), gerenciamento de fila de ofertas com calendário, auto-abastecimento inteligente, bot interativo no **Telegram**, painel web administrativo (**FastAPI**) e disparo automático para grupos do **WhatsApp** via **Evolution API**.

---

## 🔗 Repositórios do Ecossistema

O projeto é dividido em dois repositórios que trabalham em conjunto:

1. **Bot de Ofertas & Portal Web (`shopee-bot`):**
   * 📦 Repositório: <https://github.com/rodrigoicm/shopee-bot>
   * Responsável pelo Bot do Telegram, Painel Web (FastAPI), conversão de links da Shopee e Mercado Livre, banco de dados SQLite (`database.db`) e agendador automático de envios (`app_runner.py`).

2. **Evolution API & Utilitários WhatsApp (`bot-whatsapp`):**
   * 📦 Repositório: <https://github.com/rodrigoicm/bot-whatsapp>
   * Responsável por subir o servidor da **Evolution API v2** + **PostgreSQL** via Docker (`docker-compose.yml`) e pelos scripts de linha de comando (`criar_instancia.py`, `gerar_qrcode.py`, `enviar_teste.py`) para gerenciar a instância (`chip_01`) e parear o WhatsApp.

---

## 🛠️ Tecnologias Utilizadas

* **Python 3.10+**
* **FastAPI + Uvicorn** (Portal Web de Controle e Fila de Ofertas na porta `8000`)
* **python-telegram-bot** (Bot do Telegram para conversão rápida, envio aos grupos e comando `/gerar`)
* **Requests & Parsing `_n.ctx.r`** (Motor ultrarrápido de conversão e extração de dados do Mercado Livre sem navegador)
* **Playwright** (Exclusivo para extração opcional de vídeos `.mp4` de anúncios da Shopee)
* **SQLite** (`database.db` para persistência da fila de ofertas e parâmetros de horário)
* **Docker & Docker Compose** (Infraestrutura da Evolution API `v2.3.7` + PostgreSQL 16)

---

## 📦 PARTE 1: Como Subir e Configurar a Evolution API via Linha de Comando (`bot-whatsapp`)

Antes de iniciar o bot de ofertas, a **Evolution API** precisa estar rodando e com o chip conectado. Todo o processo é feito diretamente pelo terminal (PowerShell / CMD).

### 1. Clonar o repositório e subir os containers Docker

Certifique-se de que o **Docker Desktop** está aberto e execute:

```powershell
git clone https://github.com/rodrigoicm/bot-whatsapp.git
cd bot-whatsapp
```

Verifique no arquivo `docker-compose.yml` a chave definida em `AUTHENTICATION_API_KEY`:
```yaml
    environment:
      - AUTHENTICATION_API_KEY=sua_senha_global_aqui
      - SERVER_PORT=8080
```

Suba os containers em segundo plano:
```powershell
docker compose up -d
```

Verifique se os containers (`evolution-api` e `evolution_postgres`) estão rodando perfeitamente:
```powershell
docker ps
```
* A Evolution API estará ativa em: `http://localhost:8080`

---

### 2. Criar a Instância e Gerar o QR Code via Linha de Comando (CLI)

Você pode criar a instância `chip_01` e salvar o `qrcode.png` diretamente na pasta usando os scripts Python ou comando `python -c` no terminal:

#### Opção A: Comando direto no PowerShell (cria a instância `chip_01` e já salva `qrcode.png`)
```powershell
python -c "import requests, base64; r = requests.post('http://localhost:8080/instance/create', json={'instanceName': 'chip_01', 'qrcode': True, 'integration': 'WHATSAPP-BAILEYS'}, headers={'apikey': 'sua_senha_global_aqui'}); d = r.json(); b64 = d.get('qrcode', {}).get('base64', ''); open('qrcode.png', 'wb').write(base64.b64decode(b64.split(',')[1])) if b64 else print(d); print('QR Code salvo em qrcode.png!' if b64 else '')"
```

#### Opção B: Usando os scripts do repositório (`criar_instancia.py` e `gerar_qrcode.py`)
1. Edite `apikey` e `instanceName` (ex.: `"chip_01"`) dentro de `criar_instancia.py` e execute:
   ```powershell
   python criar_instancia.py
   ```
2. Caso o QR Code expire antes de ler, atualize o arquivo `qrcode.png` rodando:
   ```powershell
   python gerar_qrcode.py
   ```
3. Abra a imagem `qrcode.png` gerada na pasta:
   ```powershell
   start qrcode.png
   ```
   Escaneie o código com o seu WhatsApp (**Aparelhos Conectados > Conectar um aparelho**).

---

### 3. Comandos Úteis de Terminal para Gerenciar a Evolution API

* **Verificar se o chip está conectado (`state: "open"`):**
  ```powershell
  python -c "import requests; print(requests.get('http://localhost:8080/instance/connectionState/chip_01', headers={'apikey': 'sua_senha_global_aqui'}).json())"
  ```

* **Listar todos os Grupos do WhatsApp e descobrir o ID (`@g.us`) para colocar no `.env`:**
  ```powershell
  python -c "import requests; grupos = requests.get('http://localhost:8080/group/fetchAllGroups/chip_01?getParticipants=false', headers={'apikey': 'sua_senha_global_aqui'}).json(); [print(f\"{g.get('subject')} -> {g.get('id')}\") for g in grupos]"
  ```

* **Testar envio de mensagem pelo terminal:**
  ```powershell
  python enviar_teste.py
  ```

*(Nota: O repositório `bot-whatsapp` também conta com o painel opcional `streamlit run app.py` caso queira visualizar ou gerenciar múltiplos chips via interface gráfica).*

---

## 🤖 PARTE 2: Como Configurar e Subir o Bot de Ofertas (`shopee-bot`)

Com a Evolution API rodando na porta `8080` e a instância `chip_01` conectada, configure e inicie o projeto principal de ofertas.

### 1. Clonar o repositório e instalar as dependências

```powershell
git clone https://github.com/rodrigoicm/shopee-bot.git
cd shopee-bot
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

*(Opcional — necessário apenas se você quiser usar a extração automática de vídeos `.mp4` da Shopee no Portal Web):*
```powershell
playwright install chromium
```

### 2. Configurar o arquivo `.env`

Crie um arquivo chamado **`.env`** na raiz da pasta `shopee-bot` com a seguinte estrutura:

```env
# ==========================================================
# 1. TELEGRAM BOT (Obtido no @BotFather)
# ==========================================================
BOT_TOKEN=8963251150:AAHEtK-q-08WTZWvyfQ85u1_b6aowoImNZo

# ==========================================================
# 2. SHOPEE AFFILIATE API (GraphQL Oficial)
# ==========================================================
SHOPEE_APP_ID="18399660972"
SHOPEE_APP_SECRET="sua_chave_secreta_shopee"

# ==========================================================
# 3. EVOLUTION API & GRUPOS DO WHATSAPP
# ==========================================================
EVOLUTION_API_URL=http://localhost:8080
EVOLUTION_API_KEY=sua_senha_global_aqui
EVOLUTION_INSTANCE=chip_01

WHATSAPP_GROUP_PRINCIPAL="120363421300618666@g.us"
WHATSAPP_GROUP_PARCERIA="556596440424-1585878265@g.us"

# ==========================================================
# 4. MERCADO LIVRE AFILIADOS (Atualizado via curl_to_env.py)
# ==========================================================
ML_AFFILIATE_TAG=promocuiaba
ML_AFFILIATE_CSRF=seu_token_csrf_extraido_do_linkbuilder
ML_AFFILIATE_COOKIE='sua_string_de_cookies_do_mercadolivre'
```

> 💡 **Nota de Arquitetura:** A variável antiga `ML_COOKIES_JSON` e os scripts antigos de captura por navegador foram descontinuados e **não são mais utilizados**. Toda a comunicação do Mercado Livre agora utiliza apenas `ML_AFFILIATE_TAG`, `ML_AFFILIATE_CSRF` e `ML_AFFILIATE_COOKIE`.

---

## 🍪 Como Capturar e Atualizar a Sessão do Mercado Livre (`curl_to_env.py`)

A conversão de links do Mercado Livre é feita diretamente via requisição HTTP no endpoint interno do Linkbuilder (`/affiliate-program/api/v2/affiliates/createLink`) e a extração de dados do produto (Título, Foto, Loja, Preço Normal, Preço Pix, Desconto e Parcelamento com/sem juros) é feita lendo o JSON estruturado `_n.ctx.r` presente no código-fonte da página.

Sempre que a sua sessão do Mercado Livre expirar (erro HTTP `401`/`403`), renove as credenciais em segundos via linha de comando:

1. Acesse o [Linkbuilder de Afiliados do Mercado Livre](https://www.mercadolivre.com.br/afiliados/linkbuilder) no seu navegador (logado na sua conta).
2. Pressione **F12** para abrir o *DevTools*, clique na aba **Network** (Rede) e filtre por **Fetch/XHR**.
3. Cole qualquer link de produto do Mercado Livre no Linkbuilder e clique em **Gerar**.
4. Na lista da aba *Network*, clique com o **botão direito** sobre a requisição **`createLink`** -> **Copy** -> **Copy as cURL (bash)**.
5. Cole o conteúdo copiado dentro de um arquivo chamado **`curl.txt`** na raiz da pasta `shopee-bot`.
6. Execute no terminal:
   ```powershell
   python curl_to_env.py
   ```
7. **Pronto!** O script `curl_to_env.py` irá:
   * Extrair automaticamente `ML_AFFILIATE_COOKIE`, `ML_AFFILIATE_CSRF` e `ML_AFFILIATE_TAG`;
   * Atualizar o seu arquivo `.env` preservando todas as outras configurações;
   * Apagar o arquivo `curl.txt` automaticamente por segurança.

### 🧪 Testar a conversão do Mercado Livre via Terminal (Opcional)

Para testar qualquer link do Mercado Livre (link direto de produto, catálogo `/p/`, user product `/up/` ou link curto `meli.la` / página `/social/` de outro afiliado) direto no terminal:

```powershell
python test_ml_offer.py
```

---

## 🚀 Como Iniciar o Sistema Completo (`shopee-bot`)

Para rodar **tudo simultaneamente** (Painel Web FastAPI + Bot do Telegram + Motor de Disparos Automáticos do WhatsApp em background), execute no terminal:

```powershell
python app_runner.py
```

Assim que o comando iniciar:
* 🌐 **Painel Web Administrativo:** Acesse <http://127.0.0.1:8000>
* 🤖 **Bot do Telegram:** Já estará ativo escutando links da Shopee, links do Mercado Livre e os comandos `/gerar` ou `/auto`.
* ⏱️ **Disparador Automático (Fuso Cuiabá `-04:00`):** Monitora a fila do dia no SQLite (`database.db`), respeita a janela de horário configurada no painel (ex.: `08:00` às `22:00` a cada `30` minutos) e reabastece a fila automaticamente caso ela esvazie.

---

## 📂 Estrutura dos Arquivos Principais (`shopee-bot`)

| Arquivo | Descrição |
| :--- | :--- |
| `app_runner.py` | **Ponto de entrada principal.** Inicia o FastAPI (`portal.py`), o Bot do Telegram (`bot.py`) e o loop assíncrono de disparos automáticos. |
| `bot.py` | Lógica do Bot do Telegram (recebimento de links Shopee/ML, formatação de cópia de vendas, botões interativos para envio imediato ao WhatsApp ou inclusão na fila). |
| `portal.py` | Painel Web em FastAPI + Bootstrap 5 com cronômetro regressivo, calendário de fila por data, auto-abastecimento, teste de vídeo `.mp4` e botões de disparo manual. |
| `shopee_service.py` | Integração com a API GraphQL oficial da Shopee (`TOP_PERFORMING` + `Mais Vendidos`), geração de shortlinks e extração rápida de vídeos `.mp4`. |
| `ml_affiliate.py` | Expansão de links curtos, limpeza de parâmetros de rastreio de terceiros (`clean_url`) e geração de links de afiliado via endpoint interno `createLink`. |
| `ml_social.py` | Extrator dos dados do produto no Mercado Livre via JSON estruturado (`_n.ctx.r` / `polycards` / `JSON-LD`), capturando título, foto, loja, preço normal, preço Pix e parcelamento. |
| `ml_offer.py` | Orquestrador da oferta do Mercado Livre que une o `ml_affiliate.py` e o `ml_social.py`, entregando o dicionário completo para o `bot.py` e para o `portal.py`. |
| `curl_to_env.py` | Utilitário que lê o comando cURL da requisição `createLink` (`curl.txt`) e atualiza `ML_AFFILIATE_COOKIE`, `ML_AFFILIATE_CSRF` e `ML_AFFILIATE_TAG` no `.env`. |
| `test_ml_offer.py` | Script interativo de terminal para testar a conversão e visualizar o JSON e a prévia da mensagem do Mercado Livre. |
| `database.py` | Gerenciamento do banco SQLite (`database.db`), tabelas `queue` e `config`, controle de duplicidade de ofertas recentes e agendamento. |