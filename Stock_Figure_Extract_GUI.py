import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import yfinance as yf
import pandas as pd

class StockApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Stock Analysis Tool")
        self.root.geometry("500x350")
        
        self.input_file = None

        # UI Layout
        tk.Label(root, text="Step 1: Select your stock list file", font=('Arial', 10, 'bold')).pack(pady=5)
        self.btn_select = tk.Button(root, text="Select File", command=self.select_file)
        self.btn_select.pack(pady=5)
        
        self.file_label = tk.Label(root, text="No file selected", fg="gray")
        self.file_label.pack(pady=5)

        tk.Label(root, text="Step 2: Extract Stock Figure", font=('Arial', 10, 'bold')).pack(pady=(20, 5))
        self.btn_run = tk.Button(root, text="Run Analysis", command=self.run_process, state=tk.DISABLED)
        self.btn_run.pack(pady=5)

        self.progress = ttk.Progressbar(root, orient="horizontal", length=400, mode="determinate")
        self.progress.pack(pady=20)

        self.status_label = tk.Label(root, text="Waiting...", fg="blue")
        self.status_label.pack(pady=10)

    def select_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx")])
        if file_path:
            self.input_file = file_path
            self.file_label.config(text=f"Selected: {file_path.split('/')[-1]}", fg="black")
            self.btn_run.config(state=tk.NORMAL) 
            self.status_label.config(text="File loaded. Ready to run.")

    def run_process(self):
        output_file = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
        if not output_file:
            return

        self.btn_select.config(state=tk.DISABLED)
        self.btn_run.config(state=tk.DISABLED)
        
        threading.Thread(target=self.process_analysis, args=(self.input_file, output_file), daemon=True).start()

    def process_analysis(self, input_file, output_file):
        try:
            input_df = pd.read_excel(input_file)
            tickers = input_df.iloc[:, 0].dropna().tolist()
            total_tickers = len(tickers)
            all_data = []
            
            for i, ticker in enumerate(tickers):
                self.status_label.config(text=f"Analyzing {ticker} ({i+1}/{total_tickers})...")
                
                try:
                    stock = yf.Ticker(str(ticker))
                    # Pulling 2y as per your original code
                    data = stock.history(period="2y")
                    if data.empty:
                        continue
                        
                    data.index = data.index.tz_localize(None)

                    # --- NEW 30-DAY HIGH/LOW CALCULATION ---
                    # We use a rolling window of 30 days to get the peak and floor
                    data['High_30D'] = data['High'].rolling(window=30).max()
                    data['Low_30D'] = data['Low'].rolling(window=30).min()
                    # ---------------------------------------

                    # Your existing Indicators
                    data['MA6'] = data['Close'].rolling(window=6).mean()
                    data['MA10'] = data['Close'].rolling(window=10).mean()
                    data['MA30'] = data['Close'].rolling(window=30).mean()
                    data['MA50'] = data['Close'].rolling(window=50).mean()
                    data['MA200'] = data['Close'].rolling(window=200).mean()
                    data['Volume_MA10'] = data['Volume'].rolling(window=10).mean()
                    data['Price_Change'] = data['Close'].diff()
                    data['Pct_Change_%'] = data['Close'].pct_change() * 100
                    data['Direction'] = data['Price_Change'].apply(lambda x: 'Up' if x > 0 else ('Down' if x < 0 else 'No Change'))

                    data['Stock_Code'] = ticker
                    data['Stock_Name'] = stock.info.get('shortName', 'N/A')
                    
                    cols = ['Stock_Code', 'Stock_Name', 'High_30D', 'Low_30D'] + [c for c in data.columns if c not in ['Stock_Code', 'Stock_Name', 'High_30D', 'Low_30D']]
                    all_data.append(data[cols])
                
                except Exception as e:
                    print(f"Skipping {ticker}: {e}")
                
                self.progress['value'] = ((i + 1) / total_tickers) * 100

            if all_data:
                pd.concat(all_data).to_excel(output_file, index=True)
                self.status_label.config(text="Analysis Complete!", fg="green")
                messagebox.showinfo("Success", "Analysis complete and saved.")
            
        except Exception as e:
            messagebox.showerror("Error", str(e))
        
        finally:
            self.btn_select.config(state=tk.NORMAL)
            self.btn_run.config(state=tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    app = StockApp(root)
    root.mainloop()
