import os
from flask import Flask
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

url = os.getenv("https://jxsgpcfghlodzqebfgoy.supabase.co")
key = os.getenv("sb_publishable_XVbE5m_vWNtrFTV_jLyfWg_MpWHFa7o")

supabase = create_client(url, key)

@app.route("/")
def home():

    response = supabase.table("usuarios").select("*").execute()

    return str(response.data)

if __name__ == "__main__":
    app.run(debug=True)