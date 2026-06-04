import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import numpy as np

class StockAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Professional Stock Screener & Trend Analyzer")
        self.root.geometry("1800x950")
        
        self.df = None        
        self.portfolio = None 

        # --- Step 1: Files & Date Range ---
        frame_top = tk.LabelFrame(root, text="Step 1: Files & Date Range (Analysis Window)", padx=10, pady=10)
        frame_top.pack(fill="x", padx=10, pady=5)
        
        tk.Button(frame_top, text="Load Market Excel", command=self.load_market_file).pack(side="left", padx=5)
        self.market_label = tk.Label(frame_top, text="No Market File", fg="red")
        self.market_label.pack(side="left", padx=5)

        tk.Button(frame_top, text="Load Portfolio Excel", command=self.load_portfolio_file, bg="#e1f5fe").pack(side="left", padx=20)
        self.port_label = tk.Label(frame_top, text="No Portfolio", fg="red")
        self.port_label.pack(side="left", padx=5)
        
        tk.Label(frame_top, text="   |   From:").pack(side="left")
        self.date_from = tk.Entry(frame_top, width=12)
        self.date_from.insert(0, "2025-01-01")
        self.date_from.pack(side="left", padx=5)
        tk.Label(frame_top, text="To:").pack(side="left")
        self.date_to = tk.Entry(frame_top, width=12)
        self.date_to.insert(0, "2026-12-31")
        self.date_to.pack(side="left", padx=5)

        # --- Left Sidebar ---
        frame_left = tk.Frame(root)
        frame_left.pack(side="left", fill="y", padx=10, pady=5)

        # Step 2: Stock Code Filter
        frame_codes = tk.LabelFrame(frame_left, text="Step 2: Stock Code Filter", padx=10, pady=10)
        frame_codes.pack(fill="x")
        self.stock_filter_entry = tk.Entry(frame_codes, width=25)
        self.stock_filter_entry.pack(padx=5, pady=5)
        tk.Label(frame_codes, text="(e.g., AAOI or AAOI, AMD)", font=("Arial", 8)).pack()

        # Step 3: Trend & Breakthrough
        frame_trend = tk.LabelFrame(frame_left, text="Step 3: Trend & Breakthrough", padx=10, pady=10)
        frame_trend.pack(fill="x")
        self.breakthrough_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frame_trend, text="Price > MA30 AND MA200", variable=self.breakthrough_var).pack(anchor="w")
        tk.Label(frame_trend, text="Consecutive Days Price > MA10:").pack(anchor="w", pady=(5,0))
        self.consecutive_days_entry = tk.Entry(frame_trend, width=10)
        self.consecutive_days_entry.insert(0, "10")
        self.consecutive_days_entry.pack(anchor="w", padx=5)

        # Step 4: Growth Filters
        frame_growth = tk.LabelFrame(frame_left, text="Step 4: Growth Filters", padx=10, pady=10)
        frame_growth.pack(fill="x")
        self.pct_price_change = tk.Entry(frame_growth, width=8)
        self.pct_vol_change = tk.Entry(frame_growth, width=8)
        self.pct_vol_ma10 = tk.Entry(frame_growth, width=8) 
        tk.Label(frame_growth, text="Price % >:").grid(row=0, column=0, sticky="w")
        self.pct_price_change.grid(row=0, column=1, pady=2)
        tk.Label(frame_growth, text="Vol % >:").grid(row=1, column=0, sticky="w")
        self.pct_vol_change.grid(row=1, column=1, pady=2)
        tk.Label(frame_growth, text="Vol vs MA10 % >:").grid(row=2, column=0, sticky="w")
        self.pct_vol_ma10.grid(row=2, column=1, pady=2)

        # Step 5: Columns
        frame_vis = tk.LabelFrame(frame_left, text="Step 5: Columns", padx=10, pady=10)
        frame_vis.pack(fill="x", pady=5)
        self.vis_vars = {}
        cols_to_toggle = ["Avg Buy", "Qty", "P/L", "High 30D", "% Cur vs High", "Low 30D", "MA6", "MA10", "MA30", "MA50", "MA200", "Prev Vol", "Vol_MA10"]
        for i, col in enumerate(cols_to_toggle):
            var = tk.BooleanVar(value=True)
            self.vis_vars[col] = var
            tk.Checkbutton(frame_vis, text=col, variable=var, command=lambda c=col: self.toggle_column(c)).grid(row=i//2, column=i%2, sticky="w")

        # --- Step 6: Exclusion Filter ---
        frame_exclude = tk.LabelFrame(frame_left, text="Step 6: Exclusion Filter", padx=10, pady=10)
        frame_exclude.pack(fill="x", pady=5)
        tk.Label(frame_exclude, text="If Breakthrough found in this range,\nremove stock from results:", font=("Arial", 8, "italic"), justify="left").pack(anchor="w")
        
        ex_f_container = tk.Frame(frame_exclude)
        ex_f_container.pack(fill="x", pady=2)
        tk.Label(ex_f_container, text="From:").pack(side="left")
        self.excl_from = tk.Entry(ex_f_container, width=11)
        self.excl_from.pack(side="left", padx=2)
        
        ex_t_container = tk.Frame(frame_exclude)
        ex_t_container.pack(fill="x", pady=2)
        tk.Label(ex_t_container, text="To:    ").pack(side="left")
        self.excl_to = tk.Entry(ex_t_container, width=11)
        self.excl_to.pack(side="left", padx=2)

        tk.Button(frame_left, text="Run Analysis", command=self.analyze, bg="#4CAF50", fg="white", font=("Arial", 10, "bold")).pack(fill="x", pady=5)
        tk.Button(frame_left, text="Export Excel", command=self.export_to_excel, bg="#2196F3", fg="white").pack(fill="x")

        # --- Right Results Table ---
        frame_right = tk.Frame(root)
        frame_right.pack(side="right", fill="both", expand=True, padx=10, pady=5)
        self.cols = ("Stock", "Date", "Price", "Prev Close", "Avg Buy", "Qty", "P/L", "High 30D", 
                     "% Cur Price vs 30D High", "Low 30D", "Prev Vol", "Volume", "Vol_MA10", 
                     "MA6", "MA10", "MA30", "MA50", "MA200", "Direction")
        self.tree = ttk.Treeview(frame_right, columns=self.cols, show="headings")
        for col in self.cols:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=85, anchor="center")
        
        sb = ttk.Scrollbar(frame_right, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=sb.set)
        self.tree.pack(fill="both", expand=True)
        sb.pack(fill="x")

    def toggle_column(self, col_name):
        col_map = {
            "Avg Buy": "Avg Buy", "Qty": "Qty", "P/L": "P/L", "High 30D": "High 30D",
            "% Cur vs High": "% Cur Price vs 30D High", "Low 30D": "Low 30D",
            "MA6": "MA6", "MA10": "MA10", "MA30": "MA30", "MA50": "MA50", "MA200": "MA200",
            "Prev Vol": "Prev Vol", "Vol_MA10": "Vol_MA10"
        }
        tree_col = col_map.get(col_name, col_name)
        state = self.vis_vars[col_name].get()
        self.tree.column(tree_col, width=85 if state else 0, stretch=state)

    def load_market_file(self):
        f = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if f:
            try:
                self.df = pd.read_excel(f)
                self.df['Date'] = pd.to_datetime(self.df['Date'])
                self.market_label.config(text="Loaded Market", fg="green")
            except Exception as e: messagebox.showerror("Error", str(e))

    def load_portfolio_file(self):
        f = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if f:
            try:
                self.portfolio = pd.read_excel(f)
                self.portfolio['Ticker'] = self.portfolio['Ticker'].astype(str).str.upper()
                self.port_label.config(text="Loaded Portfolio", fg="green")
            except Exception as e: messagebox.showerror("Error", str(e))

    def apply_pct_filter(self, df, curr_col, base_col, widget):
        val = widget.get().strip()
        if not val: return df
        try:
            threshold = float(val) / 100
            return df[((df[curr_col] / df[base_col]) - 1) >= threshold]
        except: return df

    def analyze(self):
        if self.df is None: return
        
        # 1. Base Prep
        data = self.df.copy().sort_values(['Stock_Code', 'Date'])
        raw_filter = self.stock_filter_entry.get().strip().upper()
        if raw_filter:
            codes = [c.strip() for c in raw_filter.split(',') if c.strip()]
            data = data[data['Stock_Code'].astype(str).str.upper().isin(codes)]

        if data.empty:
            self.tree.delete(*self.tree.get_children())
            return

        data['Prev_Close'] = data.groupby('Stock_Code')['Close'].shift(1)
        data['Prev_Volume'] = data.groupby('Stock_Code')['Volume'].shift(1)
        
        try:
            X = int(self.consecutive_days_entry.get())
        except:
            messagebox.showerror("Error", "Enter a valid number for consecutive days.")
            return

        # 2. Logic to find breakthrough indices (Steps 1-4)
        valid_indices = []
        for code, group in data.groupby('Stock_Code'):
            group = group.sort_values('Date')
            breakthrough = (group['Close'] > group['MA30']) & (group['Close'] > group['MA200']) if self.breakthrough_var.get() else pd.Series(True, index=group.index)
            above_ma10 = (group['Close'] > group['MA10']).astype(int).tolist()
            group_indices = group.index.tolist()
            
            for i in range(len(group_indices)):
                if breakthrough.iloc[i]:
                    if i + X <= len(above_ma10):
                        streak_window = above_ma10[i : i + X]
                        if sum(streak_window) == X:
                            valid_indices.append(group_indices[i])

        # Create intermediate results from Steps 1-4
        results = data.loc[valid_indices].copy()

        # 3. Apply Exclusion Filter (Step 6)
        excl_start_str = self.excl_from.get().strip()
        excl_end_str = self.excl_to.get().strip()

        if excl_start_str and excl_end_str:
            try:
                ex_start = pd.to_datetime(excl_start_str)
                ex_end = pd.to_datetime(excl_end_str)
                
                # We check the results we FOUND so far.
                # If a stock matched the filters inside the Step 6 date range...
                excluded_stocks = results[
                    (results['Date'] >= ex_start) & (results['Date'] <= ex_end)
                ]['Stock_Code'].unique()
                
                # ...remove that stock entirely from the final results.
                results = results[~results['Stock_Code'].isin(excluded_stocks)]
            except Exception as e:
                messagebox.showwarning("Exclusion Filter Error", f"Date format error: {e}")

        # 4. Standard Date Filter (Analysis Window)
        u_start, u_end = pd.to_datetime(self.date_from.get()), pd.to_datetime(self.date_to.get())
        results = results[(results['Date'] >= u_start) & (results['Date'] <= u_end)]

        # 5. Merges & Growth Filters
        if self.portfolio is not None:
            results = results.merge(self.portfolio, left_on='Stock_Code', right_on='Ticker', how='left')
            results['PL'] = (results['Close'] - results.get('Avg_Buy_Price', 0)) * results.get('Qty', 0)
        else:
            results['Avg_Buy_Price'] = 0; results['Qty'] = 0; results['PL'] = 0

        results = self.apply_pct_filter(results, 'Close', 'Prev_Close', self.pct_price_change)
        results = self.apply_pct_filter(results, 'Volume', 'Prev_Volume', self.pct_vol_change)

        # 6. Populate Table
        self.tree.delete(*self.tree.get_children())
        for _, row in results.sort_values(['Stock_Code', 'Date'], ascending=[True, False]).iterrows():
            hi30 = row.get('High_30D', 0)
            pct_hi = ((row['Close'] / hi30) - 1) * 100 if hi30 and hi30 != 0 else 0
            self.tree.insert("", "end", values=(
                row['Stock_Code'], row['Date'].strftime('%Y-%m-%d'), round(row['Close'], 2),
                round(row.get('Prev_Close', 0), 2), round(row.get('Avg_Buy_Price', 0), 2), 
                row.get('Qty', 0), round(row.get('PL', 0), 2), hi30, f"{pct_hi:.1f}%", 
                row.get('Low_30D', 0), int(row.get('Prev_Volume', 0)), int(row['Volume']),
                int(row.get('Volume_MA10', 0)), round(row.get('MA6', 0), 2), round(row.get('MA10', 0), 2),
                round(row.get('MA30', 0), 2), round(row.get('MA50', 0), 2), round(row.get('MA200', 0), 2), 
                row.get('Direction', '-')
            ))

    def export_to_excel(self):
        items = self.tree.get_children()
        if not items:
            messagebox.showwarning("Warning", "No data to export.")
            return
        data_rows = [self.tree.item(i)['values'] for i in items]
        export_df = pd.DataFrame(data_rows, columns=self.cols)
        if 'Date' in export_df.columns:
            export_df['Date'] = pd.to_datetime(export_df['Date'])
        numeric_cols = ["Price", "Prev Close", "Avg Buy", "Qty", "P/L", "High 30D", "Low 30D", "Prev Vol", "Volume", "Vol_MA10"]
        for col in numeric_cols:
            if col in export_df.columns:
                export_df[col] = pd.to_numeric(export_df[col].astype(str).str.replace('%', ''), errors='coerce')
        f = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
        if f:
            try:
                export_df.to_excel(f, index=False)
                messagebox.showinfo("Success", "Data exported successfully!")
            except Exception as e:
                messagebox.showerror("Export Error", f"Could not save file: {e}")

if __name__ == "__main__":
    root = tk.Tk()
    app = StockAnalyzerApp(root)
    root.mainloop()
