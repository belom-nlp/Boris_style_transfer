FROM pytorch/pytorch:2.1.2-cuda12.1-cudnn8-runtime
COPY requirements.txt requirements.txt
RUN python -m pip install --upgrade pip && pip install -r requirements.txt
WORKDIR /app
COPY app.py app.py
ENV TG_BOT_TOKEN="no_such_token"
ENTRYPOINT ["python","app.py"]

