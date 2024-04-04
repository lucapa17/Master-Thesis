import os
import json
import numpy as np
import datetime
from fastapi import FastAPI, Request
from secml.array import CArray
from secml_malware.attack.blackbox.c_wrapper_phi import CEnd2EndWrapperPhi, CEmberWrapperPhi, CRemoteWrapperPhi
from secml_malware.models import MalConv, CClassifierEnd2EndMalware, CClassifierEmber, CClassifierRemote
from secml_malware.utils.is_valid_url import isValidUrl
import ember
import asyncio
import time
import hashlib

app = FastAPI()

files = []
hash_list = []
X_train = None
y_train = None
next_fine_tuning = 0
count = 0
model_version = 0

classifier = os.getenv("classifier")
labeling_service = os.getenv("labeling_service")
apiKeys = os.getenv("apiKeys")
training_samples = os.getenv("training_samples")
fine_tuning = os.getenv("fine_tuning", "false").lower() == "true"
continue_training = os.getenv("continue_training", "false").lower() == "true"
samples_for_fine_tuning = os.getenv("samples_for_fine_tuning")
load_pretrained_model = os.getenv("load_pretrained_model", "true").lower() == "true"



print(f"Classfier passed: {classifier}")

net = None
if classifier == "malconv":
    print("Loading Malconv model...")
    net = CClassifierEnd2EndMalware(MalConv())
    net.load_pretrained_model()
    net = CEnd2EndWrapperPhi(net)
    print("Malconv model loading complete.")

elif classifier == "emberGBDT":
    if load_pretrained_model:
        print("Loading Ember GBDT pretrained model")
        net = CClassifierEmber(tree_path="ember2018/ember_model_2018.txt")
        net = CEmberWrapperPhi(net)
        print("Ember GBDT pretrained model loading complete.")
    else: 
        print("Loading Ember dataset..")
        if int(training_samples) > 200000:
            subset = "train"
            file_paths = [
                "ember2018/train_features0.jsonl",
                "ember2018/train_features1.jsonl",
                "ember2018/train_features2.jsonl",
                "ember2018/train_features3.jsonl",
                "ember2018/train_features4.jsonl",
                "ember2018/train_features5.jsonl"
            ]
        else:
            subset = "test"
            file_paths = ["ember2018/test_features.jsonl"]

        X_train, y_train = ember.read_vectorized_features(
            "ember2018/",
            subset=subset,
            feature_version=2
        )
        for file_path in file_paths:
            with open(file_path, 'r') as file:
                for line in file:
                    sample = json.loads(line.strip())
                    hash_value = sample.get("sha256", None)
                    if hash_value:
                        hash_entry = {
                            "sha256": hash_value
                        }
                        hash_list.append(hash_entry)
                        
        hash_list = np.array(hash_list)
        hash_list = hash_list[y_train != -1]
        X_train = X_train[y_train != -1].astype(dtype='float64')
        y_train = y_train[y_train != -1]
        
        random_indices = np.random.choice(len(X_train), int(training_samples), replace=False)
        hash_list = hash_list[random_indices]        
        X_train = X_train[random_indices]
        y_train = y_train[random_indices]

        print("Ember dataset loading complete.")
        print("shape X_train: ", X_train.shape)
        print("shape y_train: ", y_train.shape)
        
        hash_list = list(hash_list)  
        
        print("Loading Ember GBDT model...")
        net = CClassifierEmber(X=X_train, y=y_train)
        net = CEmberWrapperPhi(net)
        print("Ember GBDT model loading complete.")
        net.classifier.save_model(f"emberGBDT_model{model_version}.txt")
else:
    raise ValueError("Classifier not specified or invalid. Please specify 'malconv' or 'emberGBDT' as the classifier.")

@app.post('/analyze')
async def analyze(request: Request):
    global count, X_train, y_train, next_fine_tuning
    
    data = await request.body()
    fp = request.headers.get("Filename")
    
    sha256_hash = hashlib.sha256()
    sha256_hash.update(data)
    hash_value = sha256_hash.hexdigest()

    if hash_value not in [entry['sha256'] for entry in hash_list]:
        bytes2CArr = np.frombuffer(data, dtype=np.uint8)
        convertedData = CArray(bytes2CArr).atleast_2d()
        prediction, confidence = net.predict(convertedData, True)
        confidence = confidence[0, 1].item()
        prediction= prediction.item()
        hash_entry = {
            "sha256": hash_value,
            "prediction": prediction,
            "confidence": confidence,    
        }
        hash_list.append(hash_entry)
        if fine_tuning :
            files.append(convertedData)
    else:
        existing_entry_index = next((i for i, entry in enumerate(hash_list) if entry['sha256'] == hash_value))
        existing_entry = hash_list[existing_entry_index]
        if existing_entry.get('prediction') is not None and existing_entry.get('confidence') is not None:
            prediction = existing_entry.get('prediction')
            confidence = existing_entry.get('confidence')
        else:
            bytes2CArr = np.frombuffer(data, dtype=np.uint8)
            convertedData = CArray(bytes2CArr).atleast_2d()
            prediction, confidence = net.predict(convertedData, True)
            confidence = confidence[0, 1].item()
            prediction= prediction.item()
            existing_entry['prediction'] = prediction
            existing_entry['confidence'] = confidence
            hash_list[existing_entry_index] = existing_entry

    count+=1
    print(f"> Request number: {count}")
    print(f"> The file named: {fp} is a {'malware' if prediction == 1 else 'goodware'}. Score: {confidence}")
        
    encoded_data = {
        "file name": fp, 
        "score": confidence,
        "time": str(datetime.datetime.now())
    }
    with open('log_file.txt', "a") as file:
        json.dump(encoded_data, file)
        file.write('\n')
       
    if fine_tuning and len(files) >= int(samples_for_fine_tuning) and time.time() >= next_fine_tuning:
        asyncio.create_task(process_fine_tuning())

    return str(confidence)

async def process_fine_tuning():
    print("Fine tuning in progress...")
    global X_train, y_train, next_fine_tuning, model_version
    X = []
    for file in files:
        X.append(np.squeeze(net.extract_features(file).tondarray()))
    y = label_data(files)
    X = np.array(X)
    y = np.array(y)

    X_train = np.concatenate((X_train, X), axis=0)
    y_train = np.concatenate((y_train, y), axis=0)
    if continue_training:
        print("Continuing training...")
        net.classifier._fit(X=X_train, y=y_train, init_model=f"emberGBDT_model{model_version}.txt")
    else:
        print("Training from scratch...")
        net.classifier._fit(X=X_train, y=y_train)

    files.clear()
    for entry in hash_list:
        if 'prediction' in entry:
            del entry['prediction']
        if 'confidence' in entry:
            del entry['confidence']
    print("Fine tuning completed.")
    model_version+=1
    net.classifier.save_model(f"emberGBDT_model{model_version}.txt")
    one_day_in_seconds = 24 * 60 * 60
    current_time = time.time()
    next_fine_tuning = current_time + one_day_in_seconds
    print("Next fine-tuning: ", datetime.datetime.fromtimestamp(next_fine_tuning).strftime('%Y-%m-%d %H:%M:%S'))

def label_data(files):
    if isValidUrl(labeling_service):
        remote_classifier = CClassifierRemote(url=labeling_service)
    else:
        remote_classifier = CClassifierRemote(antivirus=labeling_service, apiKey=apiKeys.split(","))
    remote_classifier = CRemoteWrapperPhi(remote_classifier)
    print("antivirus labeling service", labeling_service)
    print("Labeling files...")
    y_new = []
    for file in files:
        pred, confidence = remote_classifier.predict(file)
        print(f"Prediction: {'malware' if pred.item() == 1 else 'goodware'}. Score: {confidence[0, 1].item()}")
        y_new.append(pred.item())
    print("Labeling complete.")
    return y_new