FROM python:3.10-slim

ENV APP_HOME=/fund/app/fundService
WORKDIR $APP_HOME
COPY ../requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY .. .

EXPOSE 5000

RUN chmod -R 777 $APP_HOME/

CMD ["sh", "-c", "python main.py"]
