import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
from datetime import datetime
import os

class StockAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Professional Stock Screener & Portfolio Tracker")
        
        # Dynamically set size based on screen dimensions to prevent overflowing small laptop screens
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = min(1800, int(screen_width * 0.95))
        window_height = min(950, int(screen_height * 0.85))
        self.root.geometry(f"{window_width}x{window_height}")
        
        self.df = None        
        self.portfolio = None 
        self.latest_prices_dict = {} 
        self.excluded_tickers = set()

        # --- Top Section ---
        frame_top = tk.LabelFrame(root, text="Step 1: Files & Date Range", padx=10, pady=10)
        frame_top.pack(fill="x", padx=10, pady=5)
        
        tk.Button(frame_top, text="Load Market Excel", command=self.load_market_file).pack(side="left", padx=5)
        self.market_label = tk.Label(frame_top, text="No Market File", fg="red")
        self.market_label.pack(side="left", padx=5)

        tk.Button(frame_top, text="Load Portfolio Excel", command=self.load_portfolio_file, bg="#e1f5fe").pack(side="left", padx=20)
        self.port_label = tk.Label(frame_top, text="No Portfolio", fg="red")
        self.port_label.pack(side="left", padx=5)
        
        tk.Label(frame_top, text="   |   From:").pack(side="left")
        self.date_from = tk.Entry(frame_top, width=12)
        self.date_from.insert(0, "2024-01-01")
        self.date_from.pack(side="left", padx=5)
        tk.Label(frame_top, text="To:").pack(side="left")
        self.date_to = tk.Entry(frame_top, width=12)
        self.date_to.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.date_to.pack(side="left", padx=5)

        # --- Main Body Frame ---
        body_frame = tk.Frame(root)
        body_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # --- Left Section with Scrollbar ---
        canvas_container = tk.Frame(body_frame, width=320)
        canvas_container.pack(side="left", fill="y", padx=5)
        canvas_container.pack_propagate(False) 

        canvas = tk.Canvas(canvas_container, borderwidth=0, highlightthickness=0)
        v_scrollbar = tk.Scrollbar(canvas_container, orient="vertical", command=canvas.yview)
        
        # Increased canvas frame height to 1250px to comfortably hold the new filters
        frame_left = tk.Frame(canvas, width=300, height=1250) 
        frame_left.pack_propagate(False) 

        def configure_scrollregion(event):
            canvas.configure(scrollregion=(0, 0, 300, 1250))
            
        frame_left.bind("<Configure>", configure_scrollregion)
        canvas.create_window((0, 0), window=frame_left, anchor="nw")
        canvas.configure(yscrollcommand=v_scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        v_scrollbar.pack(side="right", fill="y")

        # --- Mouse Wheel Support for Sidebar Scrolling ---
        def _on_mousewheel(event):
            if event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5 or event.delta < 0:
                canvas.yview_scroll(1, "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

        # Step 2: Codes & Exclusion
        frame_codes = tk.LabelFrame(frame_left, text="Step 2: Stock Codes & Exclusion", padx=10, pady=10)
        frame_codes.pack(fill="x", pady=2)
        
        self.code_entries = [tk.Entry(frame_codes, width=10) for _ in range(4)]
        for i, e in enumerate(self.code_entries): 
            e.grid(row=i//2, column=i%2, padx=2, pady=2)

        tk.Label(frame_codes, text="--- Exclusion ---").grid(row=2, column=0, columnspan=2, pady=(10, 2))
        self.use_exclusion = tk.BooleanVar(value=True)
        tk.Checkbutton(frame_codes, text="Active Exclude List", variable=self.use_exclusion).grid(row=3, column=0, columnspan=2, sticky="w")
        
        self.exclude_path_var = tk.StringVar(value=r"C:\Users\kkych\AppData\Local\Programs\Python\Python313\Stock\Exclude_List\Excluded_list.xlsx")
        tk.Button(frame_codes, text="Set Exclude Path", command=self.browse_exclusion_file).grid(row=4, column=0, sticky="w", pady=2)
        self.exclude_status = tk.Label(frame_codes, text="List: None", fg="blue", font=("Arial", 8))
        self.exclude_status.grid(row=4, column=1, sticky="w")

        # Step 3: Technical Filters
        frame_ma = tk.LabelFrame(frame_left, text="Step 3: Technical Filters", padx=10, pady=10)
        frame_ma.pack(fill="x", pady=2)
        self.ma_vars = {}
        row_idx = 0
        for i, ma in enumerate(["MA6", "MA10", "MA30", "MA50", "MA200"]):
            v_gt, v_lt = tk.BooleanVar(), tk.BooleanVar()
            self.ma_vars[f"P > {ma}"] = (v_gt, ">", ma)
            self.ma_vars[f"P < {ma}"] = (v_lt, "<", ma)
            tk.Checkbutton(frame_ma, text=f"P > {ma}", variable=v_gt).grid(row=i, column=0, sticky="w")
            tk.Checkbutton(frame_ma, text=f"P < {ma}", variable=v_lt).grid(row=i, column=1, sticky="w")
            row_idx += 1
        
        self.ma50_gt_200 = tk.BooleanVar()
        self.ma50_lt_200 = tk.BooleanVar()
        tk.Checkbutton(frame_ma, text="MA50 > MA200", variable=self.ma50_gt_200, fg="blue").grid(row=row_idx, column=0, sticky="w")
        tk.Checkbutton(frame_ma, text="MA50 < MA200", variable=self.ma50_lt_200, fg="blue").grid(row=row_idx, column=1, sticky="w")
        row_idx += 1
        
        # --- NEW ADDITIONS FOR MA10 COMPARISONS ---
        self.ma10_gt_30 = tk.BooleanVar()
        self.ma10_lt_30 = tk.BooleanVar()
        tk.Checkbutton(frame_ma, text="MA10 > MA30", variable=self.ma10_gt_30, fg="purple").grid(row=row_idx, column=0, sticky="w")
        tk.Checkbutton(frame_ma, text="MA10 < MA30", variable=self.ma10_lt_30, fg="purple").grid(row=row_idx, column=1, sticky="w")
        row_idx += 1

        self.ma10_gt_50 = tk.BooleanVar()
        self.ma10_lt_50 = tk.BooleanVar()
        tk.Checkbutton(frame_ma, text="MA10 > MA50", variable=self.ma10_gt_50, fg="purple").grid(row=row_idx, column=0, sticky="w")
        tk.Checkbutton(frame_ma, text="MA10 < MA50", variable=self.ma10_lt_50, fg="purple").grid(row=row_idx, column=1, sticky="w")
        row_idx += 1
        
        tk.Label(frame_ma, text="Prev X days NOT meeting criteria:").grid(row=row_idx, column=0, columnspan=2, pady=(5,0))
        row_idx += 1
        self.lookback_x = tk.Entry(frame_ma, width=8)
        self.lookback_x.insert(0, "0")
        self.lookback_x.grid(row=row_idx, column=0, columnspan=2)

        # Step 4: Growth Filters
        frame_growth = tk.LabelFrame(frame_left, text="Step 4: Growth & Ratio Filters", padx=10, pady=10)
        frame_growth.pack(fill="x", pady=2)
        self.pct_price_change = tk.Entry(frame_growth, width=8)
        self.pct_vol_change = tk.Entry(frame_growth, width=8)
        self.pct_vol_ma10 = tk.Entry(frame_growth, width=8) 
        
        tk.Label(frame_growth, text="Price % >:").grid(row=0, column=0)
        self.pct_price_change.grid(row=0, column=1)
        tk.Label(frame_growth, text="Vol % >:").grid(row=1, column=0)
        self.pct_vol_change.grid(row=1, column=1)
        tk.Label(frame_growth, text="Vol vs MA10 % >:").grid(row=2, column=0)
        self.pct_vol_ma10.grid(row=2, column=1)

        tk.Label(frame_growth, text="Lat/Pri Min:").grid(row=3, column=0)
        self.lat_pri_min = tk.Entry(frame_growth, width=8)
        self.lat_pri_min.grid(row=3, column=1)
        tk.Label(frame_growth, text="Lat/Pri Max:").grid(row=4, column=0)
        self.lat_pri_max = tk.Entry(frame_growth, width=8)
        self.lat_pri_max.grid(row=4, column=1)

        tk.Label(frame_growth, text="Cut-off Rate %:", fg="darkred").grid(row=5, column=0)
        self.cutoff_rate_entry = tk.Entry(frame_growth, width=8)
        self.cutoff_rate_entry.insert(0, "5")
        self.cutoff_rate_entry.grid(row=5, column=1)

        # Step 5: Visibility
        frame_vis = tk.LabelFrame(frame_left, text="Step 5: Show/Hide Columns", padx=10, pady=10)
        frame_vis.pack(fill="x", pady=2)
        self.vis_vars = {}
        toggle_cols = ["Cut off price", "Latest Price", "Latest/Price", "Avg Buy", "Qty", "P/L", "High 30D", "% Cur Price vs 30D High", "Low 30D", "MA6", "MA10", "MA30", "MA50", "MA200", "Prev Vol", "Vol_MA10"]
        for i, col in enumerate(toggle_cols):
            var = tk.BooleanVar(value=True)
            self.vis_vars[col] = var
            tk.Checkbutton(frame_vis, text=col, variable=var, command=lambda c=col: self.toggle_column(c)).grid(row=i//2, column=i%2, sticky="w")

        # Action Buttons
        tk.Button(frame_left, text="Run Analysis", command=self.analyze, bg="#4CAF50", fg="white", font=("Arial", 10, "bold")).pack(fill="x", pady=5)
        tk.Button(frame_left, text="Export Excel", command=self.export_to_excel, bg="#2196F3", fg="white").pack(fill="x", pady=5)

        # --- Right Section ---
        frame_right = tk.LabelFrame(body_frame, text="Results", padx=10, pady=10)
        frame_right.pack(side="right", fill="both", expand=True, padx=5)

        self.cols = ("Stock", "Date", "Price", "Cut off price", "Prev Close", "Latest Price", "Latest/Price", "Avg Buy", "Qty", "P/L", "High 30D", 
                     "% Cur Price vs 30D High", "Low 30D", "Prev Vol", "Volume", "Vol_MA10", 
                     "MA6", "MA10", "MA30", "MA50", "MA200", "Direction")
        
        self.tree = ttk.Treeview(frame_right, columns=self.cols, show="headings")
        for col in self.cols:
            self.tree.heading(col, text=col)
            width = 140 if "%" in col or "/" in col else (100 if col in ["Stock", "Date", "P/L", "Cut off price"] else 80)
            self.tree.column(col, width=width, anchor="center")
        
        h_scroll = ttk.Scrollbar(frame_right, orient="horizontal", command=self.tree.xview)
        v_scroll_tree = ttk.Scrollbar(frame_right, orient="vertical", command=self.tree.yview)
        
        self.tree.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll_tree.set)
        
        v_scroll_tree.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        h_scroll.pack(fill="x")

        self.tree.tag_configure('profit', foreground='green')
        self.tree.tag_configure('loss', foreground='red')
        self.tree.tag_configure('warning', background='#ffffcc') 

        self.load_exclusion_data()

    def browse_exclusion_file(self):
        f = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx")])
        if f:
            self.exclude_path_var.set(f)
            self.load_exclusion_data()

    def load_exclusion_data(self):
        path = self.exclude_path_var.get()
        if os.path.exists(path):
            try:
                ex_df = pd.read_excel(path)
                if 'Ticker' in ex_df.columns:
                    self.excluded_tickers = set(ex_df['Ticker'].astype(str).str.upper().unique())
                    self.exclude_status.config(text=f"List: {len(self.excluded_tickers)} Loaded", fg="green")
                else:
                    messagebox.showwarning("Exclude Error", "File must have a 'Ticker' column")
            except:
                self.exclude_status.config(text="Load Fail", fg="red")
        else:
            self.exclude_status.config(text="Not Found", fg="red")

    def toggle_column(self, col_name):
        state = self.vis_vars[col_name].get()
        width = 140 if "%" in col_name or "/" in col_name else 100 if "Cut" in col_name else 80
        self.tree.column(col_name, width=width if state else 0, stretch=tk.YES if state else tk.NO)

    def load_market_file(self):
        f = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx")])
        if f:
            try:
                df = pd.read_excel(f)
                df['Date'] = pd.to_datetime(df['Date'])
                df = df.sort_values(by=['Stock_Code', 'Date'])
                self.latest_prices_dict = df.groupby('Stock_Code')['Close'].last().to_dict()
                if 'Volume_MA10' in df.columns: df = df.rename(columns={'Volume_MA10': 'Vol_MA10'})
                tech_cols = ['Close', 'MA6', 'MA10', 'MA30', 'MA50', 'MA200', 'Volume', 'Vol_MA10', 'High_30D', 'Low_30D']
                for col in tech_cols:
                    if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
                df['Prev_Close'] = df.groupby('Stock_Code')['Close'].shift(1)
                df['Prev_Volume'] = df.groupby('Stock_Code')['Volume'].shift(1)
                self.df = df
                self.market_label.config(text="Loaded Market", fg="green")
            except Exception as e:
                messagebox.showerror("Load Error", f"Failed to read file: {e}")

    def load_portfolio_file(self):
        f = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx")])
        if f:
            try:
                self.portfolio = pd.read_excel(f)
                self.portfolio['Ticker'] = self.portfolio['Ticker'].astype(str).str.upper()
                self.port_label.config(text="Loaded Portfolio", fg="green")
            except Exception as e:
                messagebox.showerror("Portfolio Error", f"Failed to read file: {e}")

    def apply_pct_filter(self, df, curr_col, base_col, widget):
        val = widget.get().strip()
        if not val: return df
        try:
            threshold = float(val) / 100
            mask = (df[base_col] != 0) & (df[base_col].notnull())
            return df[mask & (((df[curr_col] / df[base_col]) - 1) >= threshold)]
        except: return df

    def analyze(self):
        if self.df is None: 
            messagebox.showwarning("Warning", "Load Market file first!")
            return
            
        data = self.df.copy()

        # --- Exclusion Logic ---
        if self.use_exclusion.get() and self.excluded_tickers:
            data = data[~data['Stock_Code'].astype(str).str.upper().isin(self.excluded_tickers)]
        
        # --- Technical Logic ---
        criteria_met = pd.Series(True, index=data.index)
        has_active_tech_filter = False

        # Existing Price vs MA Filters
        for var, op, col in self.ma_vars.values():
            if var.get():
                has_active_tech_filter = True
                if col in data.columns:
                    if op == ">": criteria_met &= (data['Close'] > data[col])
                    else: criteria_met &= (data['Close'] < data[col])

        # Existing MA50 vs MA200 Filters
        if self.ma50_gt_200.get():
            has_active_tech_filter = True
            if 'MA50' in data.columns and 'MA200' in data.columns:
                criteria_met &= (data['MA50'] > data['MA200'])
        
        if self.ma50_lt_200.get():
            has_active_tech_filter = True
            if 'MA50' in data.columns and 'MA200' in data.columns:
                criteria_met &= (data['MA50'] < data['MA200'])

        # --- INTEGRATED MA10 FILTERS ---
        if self.ma10_gt_30.get():
            has_active_tech_filter = True
            if 'MA10' in data.columns and 'MA30' in data.columns:
                criteria_met &= (data['MA10'] > data['MA30'])

        if self.ma10_lt_30.get():
            has_active_tech_filter = True
            if 'MA10' in data.columns and 'MA30' in data.columns:
                criteria_met &= (data['MA10'] < data['MA30'])

        if self.ma10_gt_50.get():
            has_active_tech_filter = True
            if 'MA10' in data.columns and 'MA50' in data.columns:
                criteria_met &= (data['MA10'] > data['MA50'])

        if self.ma10_lt_50.get():
            has_active_tech_filter = True
            if 'MA10' in data.columns and 'MA50' in data.columns:
                criteria_met &= (data['MA10'] < data['MA50'])

        # --- Lookback Filter System ---
        if has_active_tech_filter:
            data['meets_tech'] = criteria_met
            try: x_days = int(self.lookback_x.get().strip())
            except: x_days = 0

            if x_days > 0:
                def check_previous_violation(group):
                    # Tracks if criteria was hit in the prior consecutive X days
                    prev_met_count = group['meets_tech'].shift(1).rolling(window=x_days, min_periods=x_days).sum()
                    return (group['meets_tech'] == True) & (prev_met_count == 0)
                data['final_tech_filter'] = data.groupby('Stock_Code', group_keys=False).apply(check_previous_violation)
            else:
                data['final_tech_filter'] = data['meets_tech']
            
            data = data[data['final_tech_filter'] == True]

        # --- Date Filter ---
        try:
            start_date = pd.to_datetime(self.date_from.get())
            end_date = pd.to_datetime(self.date_to.get())
            data = data[(data['Date'] >= start_date) & (data['Date'] <= end_date)]
        except: 
            messagebox.showerror("Error", "Invalid Date Range format.")
            return

        # Merge Portfolio
        if self.portfolio is not None:
            data = data.merge(self.portfolio, left_on='Stock_Code', right_on='Ticker', how='left')
            data['PL_Value'] = (data['Close'] - data['Avg_Buy_Price']) * data['Qty']
        else:
            data['Avg_Buy_Price'] = 0; data['Qty'] = 0; data['PL_Value'] = 0

        # --- Cut-off Price Calculation ---
        try:
            cutoff_pct = float(self.cutoff_rate_entry.get().strip()) / 100
        except:
            cutoff_pct = 0.0

        def calc_cutoff(row):
            avg_buy = row.get('Avg_Buy_Price', 0)
            if pd.isna(avg_buy) or avg_buy == 0: return 0.0
            if row['Close'] <= avg_buy:
                return avg_buy * (1 - cutoff_pct)
            else:
                return row.get('High_30D', 0) * (1 - cutoff_pct)

        data['Cut_Off_Val'] = data.apply(calc_cutoff, axis=1)
        data['Latest_Price_Ref'] = data['Stock_Code'].map(self.latest_prices_dict)
        data['Lat_Div_Pri'] = data['Latest_Price_Ref'] / data['Close']

        # Code & Growth Filters
        codes = [e.get().strip().upper() for e in self.code_entries if e.get().strip()]
        if codes: data = data[data['Stock_Code'].astype(str).str.upper().isin(codes)]
        data = self.apply_pct_filter(data, 'Close', 'Prev_Close', self.pct_price_change)
        data = self.apply_pct_filter(data, 'Volume', 'Prev_Volume', self.pct_vol_change)
        data = self.apply_pct_filter(data, 'Volume', 'Vol_MA10', self.pct_vol_ma10)

        try:
            min_val = self.lat_pri_min.get().strip()
            max_val = self.lat_pri_max.get().strip()
            if min_val: data = data[data['Lat_Div_Pri'] >= float(min_val)]
            if max_val: data = data[data['Lat_Div_Pri'] <= float(max_val)]
        except: pass

        # --- UI Update ---
        self.tree.delete(*self.tree.get_children())
        if data.empty:
            messagebox.showinfo("Result", "No stocks match your specific filters.")
            return

        def safe_val(val, format_type='float', decimals=2):
            if pd.isna(val): return 0 if format_type != 'str' else "N/A"
            try:
                if format_type == 'int': return int(val)
                return round(float(val), decimals)
            except: return 0

        for _, row in data.sort_values(by=['Stock_Code', 'Date']).iterrows():
            pl = row['PL_Value'] if pd.notnull(row['PL_Value']) else 0
            cutoff = row['Cut_Off_Val']
            price = row['Close']
            
            tags = []
            if pl > 0: tags.append('profit')
            elif pl < 0: tags.append('loss')
            if cutoff > 0 and price < cutoff: tags.append('warning')

            high_30d = row.get('High_30D', 1)
            high_perc = f"{(((row['Close'] / high_30d) - 1) * 100):.2f}%" if pd.notnull(high_30d) and high_30d != 0 else "0.00%"

            self.tree.insert("", "end", values=(
                row['Stock_Code'], 
                row['Date'].strftime('%Y-%m-%d'), 
                safe_val(price),
                safe_val(cutoff),
                safe_val(row['Prev_Close'], 'str'),
                safe_val(row.get('Latest_Price_Ref')),
                safe_val(row.get('Lat_Div_Pri'), decimals=4),
                safe_val(row.get('Avg_Buy_Price', 0)), 
                safe_val(row.get('Qty', 0), 'int'), 
                safe_val(pl),
                safe_val(row.get('High_30D', 0)), 
                high_perc, 
                safe_val(row.get('Low_30D', 0)), 
                safe_val(row.get('Prev_Volume'), 'str'),
                safe_val(row.get('Volume'), 'int'), 
                safe_val(row.get('Vol_MA10', 0), 'int'),
                safe_val(row.get('MA6', 0)), 
                safe_val(row.get('MA10', 0)), 
                safe_val(row.get('MA30', 0)), 
                safe_val(row.get('MA50', 0)), 
                safe_val(row.get('MA200', 0)), 
                row.get('Direction', '-')
            ), tags=tuple(tags))

    def export_to_excel(self):
        items = self.tree.get_children()
        if not items: return
        export_df = pd.DataFrame([self.tree.item(i)['values'] for i in items], columns=self.cols)
        f = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
        if f: export_df.to_excel(f, index=False)

if __name__ == "__main__":
    root = tk.Tk()
    app = StockAnalyzerApp(root)
    root.mainloop()
