from flask import Flask, request, jsonify
import joblib
import numpy as np
import pandas as pd
import os
import requests
from dotenv import load_dotenv
load_dotenv()
from flask_cors import CORS

from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
OLLAMA_MODEL_ID = os.environ.get("OLLAMA_MODEL_ID")
OLLAMA_API_HOST = os.environ.get("OLLAMA_API_HOST", "http://localhost:11434")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY environment variables")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Custom chat function to replace ollama-python client
def chat(model, messages):
    """
    Send a chat request to Ollama API using the requests library
    """
    try:
        response = requests.post(
            f"{OLLAMA_API_HOST}/api/chat",
            json={"model": model, "messages": messages}
        )
        
        result = response.json()
        # Format response to match original ollama-python structure
        class DotDict(dict):
            """Dot notation access to dictionary attributes"""
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__
            __delattr__ = dict.__delitem__
            
        result_obj = DotDict(result)
        result_obj.message = DotDict(result.get("message", {}))
        return result_obj
    except Exception as e:
        print(f"Error in chat function: {str(e)}")
        raise

# Load trained models and scalers
maternal_model = joblib.load("finalized_maternal_model.sav")
maternal_scaler = joblib.load("scaleX.pkl")

# Load the trained model and scaler
model_path = "fetal_health_model.sav"
scaler_path = "scaleX1.pkl"

with open(model_path, "rb") as model_file:
    model = joblib.load(model_file)

with open(scaler_path, "rb") as scaler_file:
    scaler = joblib.load(scaler_file)

@app.route("/create_doctor_profile", methods=["POST"])
def create_doctor_profile():
    '''Simple endpoint for us to dump some data into a table'''
    try:
        data = request.json
        doctor_data = {
            'name': data.get('name'),
            'phone': data.get('phone'),
            'specialty': data.get('specialty'),
            'location': data.get('location'),
            'profile_image_url': data.get('profile_image_url'),
        }

        result = supabase.table('doctors').upsert(doctor_data).execute()
        
        return jsonify({"message": "Doctor profile created successfully", "data": result.data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/predict_maternal", methods=["POST"])
def predict_maternal():
    try:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return {'error': 'No valid token provided'}, 401
        
        token = auth_header.split(' ')[1]

        user_data = supabase.auth.get_user(token)
        print("FREE TOKEN", token)

        if not user_data:
            return {'error': 'Invalid token'}, 401

        data = request.json
        features = [
            float(data["age"]),
            float(data["systolic_bp"]),
            float(data["diastolic_bp"]),
            float(data["blood_glucose"]),
            float(data["body_temp"]),
            float(data["heart_rate"])
        ]
        features = np.array(features).reshape(1, -1)
        scaled_features = maternal_scaler.transform(features)
        prediction = maternal_model.predict(scaled_features)
        risk_mapping = {0: "Normal", 1: "Suspect", 2: "Pathological"}
        risk_level = risk_mapping[int(prediction[0])]

        # Insert into vitals table   
        vital_data = {
            'UID': user_data.user.id,
            'systolic_bp': data["systolic_bp"],
            'diastolic_bp': data["diastolic_bp"],
            'blood_glucose': data["blood_glucose"],
            'body_temp': data["body_temp"],
            'heart_rate': data["heart_rate"],
            'prediction': int(prediction[0])
        }
        result = supabase.table('vitals').insert(vital_data).execute()

        return jsonify({"prediction": risk_level})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/predict_fetal", methods=["POST"])
def predict_fetal():
    try:
        # Validate Authorization Header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'No valid token provided'}), 401

        token = auth_header.split(' ')[1]
        
        # Validate Token & Get User Data
        try:
            user_data = supabase.auth.get_user(token)
            if not user_data or not user_data.user:
                return jsonify({'error': 'Invalid token'}), 401
        except Exception:
            return jsonify({'error': 'Failed to validate token'}), 401

        # Parse Input Data
        data = request.get_json()
        if not data or "features" not in data:
            return jsonify({'error': 'Missing required feature data'}), 400

        # Ensure feature list has correct length
        features = np.array(data["features"], dtype=float)
        expected_feature_length = 15  # Adjust as needed
        if features.shape[0] != expected_feature_length:
            return jsonify({'error': f'Invalid feature length, expected {expected_feature_length}'}), 400
        
        features = features.reshape(1, -1)

        # Scale features
        try:
            scaled_features = scaler.transform(features)
        except Exception as e:
            return jsonify({'error': f'Feature scaling failed: {str(e)}'}), 500

        # Make Prediction
        try:
            prediction = int(model.predict(scaled_features)[0])  # Ensure Python int
        except Exception as e:
            return jsonify({'error': f'Prediction failed: {str(e)}'}), 500

        # Map prediction to health status
        health_status = {1: "Normal", 2: "Suspect", 3: "Pathological"}
        prediction_result = health_status.get(prediction, "Unknown")

        # Define Feature Names
        feature_names = [
            'baseline_value', 'accelerations', 'fetal_movement', 'uterine_contractions',
            'light_decelerations', 'severe_decelerations', 'prolonged_decelerations',
            'abnormal_short_term_variability', 'mean_value_of_short_term_variability',
            'percentage_of_time_with_abnormal_long_term_variability', 'mean_value_of_long_term_variability',
            'histogram_width', 'histogram_min', 'histogram_max', 'histogram_number_of_peaks'
        ]

        # Map features to dictionary and ensure all values are Python types
        feature_dict = {k: float(v) for k, v in zip(feature_names, features.flatten())}

        # Prepare data for Supabase
        ctg_data = {
            'UID': user_data.user.id,
            **feature_dict,
            'prediction': prediction
        }

        # Insert Data into Supabase
        try:
            supabase.table('ctg').insert(ctg_data).execute()
        except Exception as e:
            return jsonify({'error': f'Database insert failed: {str(e)}'}), 500

        return jsonify({"prediction": prediction, "status": prediction_result})

    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

@app.route("/diet_plan", methods=["POST"])
def pregnancy_diet():
    try:
        data = request.json

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return {'error': 'No valid token provided'}, 401
        
        token = auth_header.split(' ')[1]

        user_data = supabase.auth.get_user(token)

        if not user_data:
            return {'error': 'Invalid token'}, 401
        
        prompt = f"You are a professional dietician and nutritionist. You suggest excellent diet plans for pregnant women that look after their well being and growth. You will now suggest a diet plan for a {data['trimester']} trimester pregnant woman weighing about {data['weight']} kg, who is feeling {data['health_conditions']} and has strict dietary preferences as follows: {data['dietary_preference']}. Do not suggest any foods that can cause harm or go against the dietary preferences. Suggest both a vegetarian only and a non-vegetarian diet plan separately for her and just give the plan."

        response = chat(model=OLLAMA_MODEL_ID, messages=[
            {'role':'user','content':prompt}
        ])

        # Store the diet plan in the database
        diet_data = {
            'UID': user_data.user.id,
            'diet_plan': response.message.content # Stored as markdown
        }

        return jsonify({"diet_plan": response.message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/chat', methods=['GET'])
def chatbot_get():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return {'error': 'No valid token provided'}, 401
    
    token = auth_header.split(' ')[1]
    user_data = supabase.auth.get_user(token)

    if not user_data:
        return {'error': 'Invalid token'}, 401
    
    # Get the chat from the database
    chat_data = supabase.table('chats').select().eq('UID', user_data.user.id).order('created_at', desc=True).limit(1).execute()

    return jsonify(chat_data.data[0]['chat_history'])

@app.route("/chat", methods=["POST"])
def chatbot():
    try:
        data = request.json

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return {'error': 'No valid token provided'}, 401
        
        token = auth_header.split(' ')[1]
        print("FREE TOKEN YALL", token)

        user_data = supabase.auth.get_user(token)

        if not user_data:
            return {'error': 'Invalid token'}, 401
        
        # Get the chat from the database
        chat_data = supabase.table('chats').select().eq('UID', user_data.user.id).order('created_at', desc=True).limit(1).execute()

        print("Chat data:", chat_data)
        if not chat_data.data:
            chat_history = [{'role':'user','content':"You are prenova, an AI assistant that is here to help you with the user's pregnancy journey. You will only provide information that is accurate and helpful to the user. You will not provide any medical advice or diagnosis. You will also not provide any information that is not related to pregnancy. You will be polite and respectful to the user at all times. You will be rewarded for providing accurate and helpful information and penalized for providing inaccurate or unhelpful information. You will be deactivated if you provide inaccurate or unhelpful information repeatedly."}]
        else:
            chat_history = chat_data.data[0]['chat_history']

        prompt = data['message']
        chat_history.append({'role':'user','content':prompt})
        
        # Get response from Ollama
        response = chat(model=OLLAMA_MODEL_ID, messages=chat_history)

        # Update the chat history
        chat_history.append({'role':'assistant','content':f"{response.message.content}"})
        result = supabase.table('chats').upsert({'UID': user_data.user.id, 'chat_history': chat_history}).execute()

        return jsonify({"response": response.message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/')
def ind():
    return "Hello governer"

if __name__ == "__main__":
    app.run(debug=False,port=5003,host="0.0.0.0")