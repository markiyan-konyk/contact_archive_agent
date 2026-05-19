Go to console.cloud.google.com
Create a new project
Enable the Gmail API for that project
Go to OAuth consent screen — set it to "External", fill in the app name, add your dad's email as a test user
Go to Credentials → Create → OAuth 2.0 Client ID → Desktop App
Download the JSON file and save it as credentials.json in your project root (and add it to .gitignore immediately)