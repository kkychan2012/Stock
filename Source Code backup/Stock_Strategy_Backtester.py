import pandas as pd
import numpy as np
import os
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

class StockApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Stock Screener Strategy Analyzer (Multi-Tab Excel)")
        self.root.geometry("1200x800")
        self.root.minsize(1050, 700)
        
        self.raw_dataframe = None
        self.uploaded_file_path = None
        
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(0, weight=4) 
        main_frame.columnconfigure(1, weight=5) 
        main_frame.rowconfigure(0, weight=1)

        # ------------------ LEFT SIDE PANEL ------------------
        left_container = ttk.Frame(main_frame)
        left_container.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_container.columnconfigure(0, weight=1)
        left_container.rowconfigure(2, weight=1) 

        # Step 1: Data Source
        data_panel = ttk.LabelFrame(left_container, text=" Step 1: Historical Data Source ", padding="10")
        data_panel.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        self.btn_upload = ttk.Button(data_panel, text="Upload Master Stock Data (CSV/Excel)", command=self.upload_master_file)
        self.btn_upload.pack(fill="x", pady=(0, 2))
        
        self.lbl_status = ttk.Label(data_panel, text="No master historical file loaded.", foreground="red", font=("Helvetica", 9, "italic"))
        self.lbl_status.pack(anchor="w")

        # Step 2: Configurations Notebook
        config_notebook = ttk.Notebook(left_container)
        config_notebook.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        
        # TAB A: Single Mode & Customization
        tab_single = ttk.Frame(config_notebook, padding="10")
        config_notebook.add(tab_single, text=" Single Mode & Strategy Customization ")
        
        stock_frame = ttk.LabelFrame(tab_single, text=" Core Parameters ", padding="5")
        stock_frame.pack(fill="x", pady=(0, 5))
        stock_frame.columnconfigure(1, weight=1)
        stock_frame.columnconfigure(3, weight=1)
        
        ttk.Label(stock_frame, text="Ticker:").grid(row=0, column=0, sticky="w", pady=2, padx=2)
        self.entry_stock = ttk.Entry(stock_frame, width=10)
        self.entry_stock.grid(row=0, column=1, sticky="ew", pady=2, padx=5)
        self.entry_stock.insert(0, "TSLA")
        
        ttk.Label(stock_frame, text="Entry P0 ($):").grid(row=0, column=2, sticky="w", pady=2, padx=2)
        self.entry_price = ttk.Entry(stock_frame, width=10)
        self.entry_price.grid(row=0, column=3, sticky="ew", pady=2, padx=5)
        self.entry_price.insert(0, "100.00")
        
        ttk.Label(stock_frame, text="Start Date:").grid(row=1, column=0, sticky="w", pady=2, padx=2)
        self.entry_start = ttk.Entry(stock_frame)
        self.entry_start.grid(row=1, column=1, sticky="ew", pady=2, padx=5)
        self.entry_start.insert(0, "2024-07-02")
        
        ttk.Label(stock_frame, text="End Date:").grid(row=1, column=2, sticky="w", pady=2, padx=2)
        self.entry_end = ttk.Entry(stock_frame)
        self.entry_end.grid(row=1, column=3, sticky="ew", pady=2, padx=5)
        self.entry_end.insert(0, "2026-12-31")
        
        # Percentages
        pct_frame = ttk.LabelFrame(tab_single, text=" Strategy Percentage Modifiers (%) ", padding="5")
        pct_frame.pack(fill="x", pady=5)
        pct_frame.columnconfigure(1, weight=1)
        pct_frame.columnconfigure(3, weight=1)
        
        ttk.Label(pct_frame, text="Stage 1 Target (+%):").grid(row=0, column=0, sticky="w", pady=2, padx=2)
        self.entry_t1 = ttk.Entry(pct_frame, width=8)
        self.entry_t1.grid(row=0, column=1, sticky="ew", pady=2, padx=5)
        self.entry_t1.insert(0, "10.0")
        
        ttk.Label(pct_frame, text="Stage 1 Stop Loss (-%):").grid(row=0, column=2, sticky="w", pady=2, padx=2)
        self.entry_sl = ttk.Entry(pct_frame, width=8)
        self.entry_sl.grid(row=0, column=3, sticky="ew", pady=2, padx=5)
        self.entry_sl.insert(0, "11.0")
        
        ttk.Label(pct_frame, text="Stage 2 Target (+%):").grid(row=1, column=0, sticky="w", pady=2, padx=2)
        self.entry_t2 = ttk.Entry(pct_frame, width=8)
        self.entry_t2.grid(row=1, column=1, sticky="ew", pady=2, padx=5)
        self.entry_t2.insert(0, "20.0")
        
        ttk.Label(pct_frame, text="Stage 2 Reversal (%):").grid(row=1, column=2, sticky="w", pady=2, padx=2)
        self.entry_rev = ttk.Entry(pct_frame, width=8)
        self.entry_rev.grid(row=1, column=3, sticky="ew", pady=2, padx=5)
        self.entry_rev.insert(0, "0.0")
        
        ttk.Label(pct_frame, text="Stage 3 Protection (+%):").grid(row=2, column=0, sticky="w", pady=2, padx=2)
        self.entry_prot = ttk.Entry(pct_frame, width=8)
        self.entry_prot.grid(row=2, column=1, sticky="ew", pady=2, padx=5)
        self.entry_prot.insert(0, "10.0")
        
        ttk.Label(pct_frame, text="Trailing Stop Drop (-%):").grid(row=2, column=2, sticky="w", pady=2, padx=2)
        self.entry_trail = ttk.Entry(pct_frame, width=8)
        self.entry_trail.grid(row=2, column=3, sticky="ew", pady=2, padx=5)
        self.entry_trail.insert(0, "10.0")
        
        self.btn_run_single = ttk.Button(tab_single, text="Run Single Strategy Backtest", command=self.process_single_backtest, state="disabled")
        self.btn_run_single.pack(fill="x", ipady=2, pady=(5, 0))
        
        # TAB B: Batch Mode
        tab_batch = ttk.Frame(config_notebook, padding="10")
        config_notebook.add(tab_batch, text=" Batch Processing Mode ")
        
        self.btn_run_batch = ttk.Button(tab_batch, text="Upload Batch File & Execute Batch Processing", command=self.process_batch_backtest, state="disabled")
        self.btn_run_batch.pack(fill="x", ipady=5, pady=15)

        # Output Log Panel
        results_panel = ttk.LabelFrame(left_container, text=" Backtest Activity Log & Performance Summary ", padding="12")
        results_panel.grid(row=2, column=0, sticky="nsew")
        
        self.results_text = tk.Text(results_panel, wrap="none", font=("Consolas", 9), bg="#ffffff", relief="sunken", bd=1)
        scroll_y = ttk.Scrollbar(results_panel, orient="vertical", command=self.results_text.yview)
        scroll_x = ttk.Scrollbar(results_panel, orient="horizontal", command=self.results_text.xview)
        self.results_text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.pack(side="right", fill="y")
        scroll_x.pack(side="bottom", fill="x")
        self.results_text.pack(expand=True, fill="both")

        # ------------------ RIGHT SIDE PANEL ------------------
        right_panel = ttk.LabelFrame(main_frame, text=" Live Dynamic Strategy Target Matrix ", padding="15")
        right_panel.grid(row=0, column=1, sticky="nsew")
        
        self.strategy_text = tk.Text(right_panel, wrap="word", font=("Consolas", 10), bg="#f8f9fa", relief="flat")
        self.strategy_text.pack(expand=True, fill="both")
        
        for widget in (self.entry_price, self.entry_stock, self.entry_t1, self.entry_sl, self.entry_t2, self.entry_rev, self.entry_prot, self.entry_trail):
            widget.bind("<KeyRelease>", self.update_strategy_display)
        
        self.update_strategy_display()

    def get_strategy_percentages(self):
        try:
            return {
                't1': float(self.entry_t1.get()) / 100.0,
                'sl': float(self.entry_sl.get()) / 100.0,
                't2': float(self.entry_t2.get()) / 100.0,
                'rev': float(self.entry_rev.get()) / 100.0,
                'prot': float(self.entry_prot.get()) / 100.0,
                'trail': float(self.entry_trail.get()) / 100.0
            }
        except ValueError:
            return {'t1': 0.10, 'sl': 0.11, 't2': 0.20, 'rev': 0.0, 'prot': 0.10, 'trail': 0.10}

    def update_strategy_display(self, event=None):
        try: p_0 = float(self.entry_price.get())
        except ValueError: p_0 = 0.0
        ticker = self.entry_stock.get().strip().upper()
        rules = self.get_strategy_percentages()
            
        self.strategy_text.config(state="normal")
        self.strategy_text.delete("1.0", tk.END)
        
        blueprint = f"""============================================================
STRATEGY EXECUTION MATRIX (Initial Position: 4 QTY)
============================================================
[Target Definition]
• Active Stock Ticker : {ticker if ticker else "NOT SPECIFIED"}
• Planned Entry P0    : ${p_0:.2f}

------------------------------------------------------------
STAGE 1: FULL POSITION (Holding 4 QTY)
------------------------------------------------------------
▲ Target 1 (+{rules['t1']*100:.1f}%)  : Sell 2 QTY if High >= ${p_0 * (1 + rules['t1']):.2f}
▼ Downside (-{rules['sl']*100:.1f}%)  : Sell ALL 4 QTY if Low <= ${p_0 * (1 - rules['sl']):.2f} [Stop Loss]

------------------------------------------------------------
STAGE 2: PARTIAL RUNNER (Holding 2 QTY remaining)
------------------------------------------------------------
▲ Target 2 (+{rules['t2']*100:.1f}%)  : Sell 1 QTY if High >= ${p_0 * (1 + rules['t2']):.2f}
▼ Reversal ({rules['rev']*100:.1f}%)   : Sell remaining 2 QTY if Low <= ${p_0 * (1 + rules['rev']):.2f}

------------------------------------------------------------
STAGE 3: FINAL POSITION (Holding 1 QTY remaining)
------------------------------------------------------------
▼ Protection (+{rules['prot']*100:.1f}%): Sell final 1 QTY if Low <= ${p_0 * (1 + rules['prot']):.2f}
▼ Trailing Stop (-{rules['trail']*100:.1f}%): Sell final 1 QTY if Low <= (High_30D * {1 - rules['trail']:.2f})
"""
        self.strategy_text.insert("1.0", blueprint)
        self.strategy_text.config(state="disabled")

    def upload_master_file(self):
        file_path = filedialog.askopenfilename(title="Select Master Historical Price Data File", filetypes=[("Excel Files", "*.xlsx *.xls"), ("CSV Files", "*.csv")])
        if not file_path: return 
        try:
            df = pd.read_csv(file_path) if file_path.endswith('.csv') else pd.read_excel(file_path)
            df.columns = [c.strip() for c in df.columns]
            df['Date'] = pd.to_datetime(df['Date'])
            self.raw_dataframe = df
            self.uploaded_file_path = file_path
            self.lbl_status.config(text=f"Loaded Master: {os.path.basename(file_path)}", foreground="green")
            self.btn_run_single.config(state="normal")
            self.btn_run_batch.config(state="normal")
        except Exception as e:
            messagebox.showerror("Import Error", str(e))

    def run_trading_simulation(self, df_stock, p_0, start_date, rules):
        df_stock = df_stock.sort_values('Date').reset_index(drop=True)
        df_stock = df_stock[df_stock['Date'] >= pd.to_datetime(start_date)].reset_index(drop=True)
        
        if len(df_stock) == 0: return []
            
        status = 'HOLDING_4' 
        remaining_qty = 4
        last_processed_price = p_0
        last_date_str = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        
        transactions = [{
            'Date': last_date_str, 'Action': 'BUY', 'Qty': 4, 'Price': p_0, 
            'Cash_Flow': -4 * p_0, 'Remaining': 4, 'Reason': 'Initial Position Setup'
        }]
        
        for idx, row in df_stock.iterrows():
            if status == 'FULLY_SOLD': break
                
            last_date_str = row['Date'].strftime('%Y-%m-%d')
            high, low = float(row['High']), float(row['Low'])
            last_processed_price = float(row.get('Open', high)) 
            high_30d = float(row['High_30D']) if not pd.isna(row['High_30D']) else high
            
            if status == 'HOLDING_4':
                if low <= p_0 * (1 - rules['sl']):
                    transactions.append({'Date': last_date_str, 'Action': 'SELL', 'Qty': 4, 'Price': p_0 * (1 - rules['sl']), 'Cash_Flow': 4 * p_0 * (1 - rules['sl']), 'Remaining': 0, 'Reason': 'Stop Loss Hit'})
                    remaining_qty = 0
                    status = 'FULLY_SOLD'
                elif high >= p_0 * (1 + rules['t1']):
                    transactions.append({'Date': last_date_str, 'Action': 'SELL', 'Qty': 2, 'Price': p_0 * (1 + rules['t1']), 'Cash_Flow': 2 * p_0 * (1 + rules['t1']), 'Remaining': 2, 'Reason': 'Profit Target 1 Hit'})
                    remaining_qty = 2
                    status = 'HOLDING_2'
                    
            elif status == 'HOLDING_2':
                if low <= p_0 * (1 + rules['rev']):
                    transactions.append({'Date': last_date_str, 'Action': 'SELL', 'Qty': 2, 'Price': p_0 * (1 + rules['rev']), 'Cash_Flow': 2 * p_0 * (1 + rules['rev']), 'Remaining': 0, 'Reason': 'Reversal Trigger Hit'})
                    remaining_qty = 0
                    status = 'FULLY_SOLD'
                elif high >= p_0 * (1 + rules['t2']):
                    transactions.append({'Date': last_date_str, 'Action': 'SELL', 'Qty': 1, 'Price': p_0 * (1 + rules['t2']), 'Cash_Flow': 1 * p_0 * (1 + rules['t2']), 'Remaining': 1, 'Reason': 'Profit Target 2 Hit'})
                    remaining_qty = 1
                    status = 'HOLDING_1'
                    
            elif status == 'HOLDING_1':
                if low <= p_0 * (1 + rules['prot']):
                    transactions.append({'Date': last_date_str, 'Action': 'SELL', 'Qty': 1, 'Price': p_0 * (1 + rules['prot']), 'Cash_Flow': 1 * p_0 * (1 + rules['prot']), 'Remaining': 0, 'Reason': 'Protection Threshold Hit'})
                    remaining_qty = 0
                    status = 'FULLY_SOLD'
                elif low <= high_30d * (1 - rules['trail']):
                    transactions.append({'Date': last_date_str, 'Action': 'SELL', 'Qty': 1, 'Price': high_30d * (1 - rules['trail']), 'Cash_Flow': 1 * high_30d * (1 - rules['trail']), 'Remaining': 0, 'Reason': 'Trailing Stop Hit'})
                    remaining_qty = 0
                    status = 'FULLY_SOLD'

        if remaining_qty > 0:
            transactions.append({
                'Date': last_date_str, 'Action': 'OPEN HOLDING', 'Qty': remaining_qty, 'Price': last_processed_price, 
                'Cash_Flow': remaining_qty * last_processed_price, 'Remaining': remaining_qty, 'Reason': 'Horizon Cap (Asset Open)'
            })

        return transactions

    def calculate_metrics(self, txs):
        df_tx = pd.DataFrame(txs)
        initial_cost = abs(df_tx[df_tx['Action'] == 'BUY']['Cash_Flow'].sum())
        total_returns = df_tx[df_tx['Action'] != 'BUY']['Cash_Flow'].sum()
        total_pnl = total_returns - initial_cost
        roi = (total_pnl / initial_cost) * 100 if initial_cost > 0 else 0
        return initial_cost, total_pnl, roi

    def process_single_backtest(self):
        if self.raw_dataframe is None: return
        self.results_text.delete("1.0", tk.END)
        try:
            custom_p0 = float(self.entry_price.get())
            target_stock = self.entry_stock.get().strip().upper()
            start_dt = pd.to_datetime(self.entry_start.get())
            end_dt = pd.to_datetime(self.entry_end.get())
            rules = self.get_strategy_percentages()
        except Exception: return

        df = self.raw_dataframe.copy()
        mask = (df['Stock_Code'].str.strip().str.upper() == target_stock) & (df['Date'] >= start_dt) & (df['Date'] <= end_dt)
        filtered_df = df[mask]
        if len(filtered_df) == 0: return
        
        all_txs = self.run_trading_simulation(filtered_df, custom_p0, start_dt, rules)
        initial_cost, total_pnl, roi = self.calculate_metrics(all_txs)
        
        log_lines = [
            f"=====================================================",
            f" BACKTEST SIMULATION RESULTS: {target_stock}",
            f"=====================================================",
            f"• Initial Outlay Cost : ${initial_cost:,.2f}",
            f"• Total Profit / Loss : ${total_pnl:,.2f}",
            f"• Strategy Return ROI : {roi:.2f}%\n",
            f"--- TIMELINE DETAIL LOGS ---"
        ]
        for t in all_txs:
            marker = "🟢" if t['Action'] == 'BUY' else ("🔵" if t['Action'] == 'OPEN HOLDING' else "🔴")
            log_lines.append(f"{marker} [{t['Date']}] {t['Action']} -> {t['Qty']} QTY @ ${t['Price']:.2f} | {t['Reason']}")
            
        self.results_text.insert("1.0", "\n".join(log_lines))

    def process_batch_backtest(self):
        if self.raw_dataframe is None: return
        batch_file_path = filedialog.askopenfilename(title="Select Batch Configuration File", filetypes=[("Excel Files", "*.xlsx *.xls"), ("CSV Files", "*.csv")])
        if not batch_file_path: return
        
        try:
            df_batch = pd.read_csv(batch_file_path) if batch_file_path.endswith('.csv') else pd.read_excel(batch_file_path)
            df_batch.columns = [c.strip() for c in df_batch.columns]
            rules = self.get_strategy_percentages()
            
            self.results_text.delete("1.0", tk.END)
            summary_console = []
            
            master_tx_rows = []
            performance_summary_rows = []
            
            for idx, batch_row in df_batch.iterrows():
                ticker = str(batch_row['Stock']).strip().upper()
                p_0 = float(batch_row['Price'])
                start_date = pd.to_datetime(batch_row['Date'])
                start_date_str = start_date.strftime('%Y-%m-%d')
                
                df_master = self.raw_dataframe.copy()
                filtered_df = df_master[df_master['Stock_Code'].str.strip().str.upper() == ticker]
                
                if len(filtered_df) == 0: continue
                txs = self.run_trading_simulation(filtered_df, p_0, start_date, rules)
                if not txs: continue
                
                initial_cost, total_pnl, roi = self.calculate_metrics(txs)
                
                # Dynamic Inventory Calculations for the Summary Tab
                qty_sold = sum(t['Qty'] for t in txs if t['Action'] == 'SELL')
                qty_remaining = next((t['Qty'] for t in txs if t['Action'] == 'OPEN HOLDING'), 0)
                
                # Get the absolute final price item recorded in this run
                final_action_price = txs[-1]['Price']
                
                has_holding = any(t['Action'] == 'OPEN HOLDING' for t in txs)
                status_tag = "Active Holding" if has_holding else "Closed"
                
                # 1. Store full timeline rows for Tab 1
                for t in txs:
                    master_tx_rows.append({
                        'Ticker': ticker,
                        'Date': t['Date'],
                        'Action': t['Action'],
                        'Qty': t['Qty'],
                        'Price': t['Price'],
                        'Cash_Flow': t['Cash_Flow'],
                        'Remaining_Inventory': t['Remaining'],
                        'Reason': t['Reason']
                    })
                
                # 2. Store specialized KPI row for Tab 2 (Performance Summary)
                performance_summary_rows.append({
                    'Stock_Code': ticker,
                    'Buying_Date': start_date_str,
                    'Initial_Price_P0': p_0,
                    'Today_Selling_Price': final_action_price,
                    'QTY_Sold': qty_sold,
                    'QTY_Remaining': qty_remaining,
                    'Net_Profit_Loss': total_pnl,
                    'ROI_Percentage': round(roi, 2),
                    'Current_Status': status_tag
                })
                
                summary_console.append(f"✅ {ticker} -> P0: ${p_0:.2f} | Net P/L: ${total_pnl:,.2f} ({roi:.2f}%) [{status_tag}]")
            
            # Print to local UI window text layout box
            self.results_text.insert(tk.END, "=====================================================\n")
            self.results_text.insert(tk.END, "           BATCH PROCESSING ACCOUNTING SUMMARY       \n")
            self.results_text.insert(tk.END, "=====================================================\n")
            self.results_text.insert(tk.END, "\n".join(summary_console))
            
            # Write out to Excel workbook worksheets
            if master_tx_rows:
                df_tab1_tx = pd.DataFrame(master_tx_rows)
                df_tab2_perf = pd.DataFrame(performance_summary_rows)
                
                export_path = os.path.join(os.path.dirname(self.uploaded_file_path), "PORTFOLIO_BATCH_ACCURATE_RESULTS.xlsx")
                
                with pd.ExcelWriter(export_path, engine='openpyxl') as writer:
                    df_tab1_tx.to_excel(writer, sheet_name='Transaction_Logs', index=False)
                    df_tab2_perf.to_excel(writer, sheet_name='Performance_Summary', index=False)
                
                self.results_text.insert(tk.END, f"\n\n💾 Master Multi-Tab Workbook saved at:\n{export_path}")
                messagebox.showinfo("Success", "Excel multi-tab batch generation completed successfully.")
                
        except Exception as e:
            messagebox.showerror("Batch Anomaly", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    app = StockApp(root)
    root.mainloop()
