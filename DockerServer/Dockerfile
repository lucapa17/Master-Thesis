FROM python:3.9-slim
WORKDIR /app
RUN apt-get update && apt-get install -y git libmagic1 nano wget bzip2 libgomp1
COPY requirements.txt /tmp
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN wget https://ember.elastic.co/ember_dataset_2018_2.tar.bz2 && \
    tar -xvf ember_dataset_2018_2.tar.bz2 && \
    rm ember_dataset_2018_2.tar.bz2
COPY create_vectorized_features.py .
RUN python create_vectorized_features.py
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]