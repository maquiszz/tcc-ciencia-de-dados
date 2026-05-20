from supabase import create_client

url = "https://jxsgpcfghlodzqebfgoy.supabase.co"

key = "sb_publishable_XVbE5m_vWNtrFTV_jLyfWg_MpWHFa7o"

supabase = create_client(url, key)

print("Conectado")