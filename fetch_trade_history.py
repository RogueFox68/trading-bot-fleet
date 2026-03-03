import requests
import pandas as pd
import datetime
import config

def fetch_history():
    print("Connecting to Alpaca API using local config keys...")
    
    # Determine URL based on config paper flag
    base_url = "https://paper-api.alpaca.markets" if config.PAPER else "https://api.alpaca.markets"
    url = f"{base_url}/v2/account/activities/FILL"
    
    headers = {
        "APCA-API-KEY-ID": config.API_KEY,
        "APCA-API-SECRET-KEY": config.SECRET_KEY,
        "accept": "application/json"
    }
    
    # Calculate 7 days ago timestamp in ISO-8601 format
    seven_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
    
    params = {
        "after": seven_days_ago,
        "direction": "asc",
        "page_size": 100
    }
    
    print(f"Fetching FILL activities since {seven_days_ago} via raw HTTP request...")
    
    all_activities = []
    
    while True:
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            activities = response.json()
            
            if not activities:
                break
                
            all_activities.extend(activities)
            
            # Alpaca v2 activities pagination uses a page_token in the response headers 
            # Or you can just use the 'id' of the last activity as the page_token
            # Let's check headers for page_token
            page_token = response.headers.get('pe-page-token') or response.headers.get('x-alpaca-page-token')
            
            # If no token via header, use the time of the last activity
            if not page_token:
                # since we are fetching 'asc', the last item is the newest
                last_time = activities[-1]['transaction_time']
                params['after'] = last_time
                # sleep slightly to avoid rate limit if we have to page a lot
                import time; time.sleep(0.1)
                
                # To avoid infinite loop on same timestamp
                if len(activities) < 100:
                    break
            else:
                params['page_token'] = page_token
                
        except Exception as e:
            print(f"Failed to fetch activities. HTTP Error: {e}")
            if 'response' in locals() and response.text:
                print(f"Response: {response.text}")
            break

    if not all_activities:
        print("No trade activities found in the last 7 days.")
        return
        
    print(f"Found {len(all_activities)} FILL activities.")
    
    # Turn the activities list of dictionaries into a pandas dataframe
    activities_df = pd.DataFrame(all_activities)
    
    # Sort by transaction time for easier reading
    if 'transaction_time' in activities_df.columns:
        activities_df = activities_df.sort_values(by='transaction_time', ascending=False)
    
    # Generate timestamped filename
    filename = f'alpaca_trades_7days_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    
    # Use Pandas handy `to_csv` method to write the dataframe to a file
    activities_df.to_csv(filename, index=False)
    
    print(f"Successfully exported data to: {filename}")

if __name__ == "__main__":
    fetch_history()
