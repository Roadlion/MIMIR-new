import yfinance as tk

# Define the company's ticker symbol
company = tk.Ticker("AAPL")

# Fetch all available company metadata
company_info = company.info

# Print specific company data (e.g., sector or business summary)
print(company_info.get('sector'))
print(company_info.get('longBusinessSummary'))

# Fetch financial statements
income_statement = company.income_stmt

print(income_statement)