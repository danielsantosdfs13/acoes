# Build a partir da RAIZ do repo: docker build -f deploy/streamlit.Dockerfile ..
# (o serviço "streamlit" no docker-compose.yml já usa context: .. + este Dockerfile).
#
# Instala requirements.txt + requirements-homelab.txt — NÃO instala
# requirements-local.txt (MetaTrader5): este container é Linux e nunca
# seleciona source="MetaTrader 5"; esse modo continua existindo só para
# quem rodar streamlit_app.py na própria VM Windows (ver README.md).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-homelab.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-homelab.txt

COPY daytrade_smc.py streamlit_app.py ./

EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0"]
