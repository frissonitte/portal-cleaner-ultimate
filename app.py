import os
import threading
import time
import tkinter as tk
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


@dataclass
class Config:
    BASE_URL = "http://192.168.1.248/"
    START_URL = BASE_URL + "Uretim/UrunNerede"
    WAIT_TIMEOUT: int = 10
    PAGE_LOAD_DELAY: int = 15
    SEARCH_DELAY: int = 2
    CLICK_DELAY: int = 1
    UPDATE_DELAY: int = 2
    MAX_RETRIES: int = 3
    ERROR_FILE: str = "error_urunler.txt"

    ERROR_KEYWORDS: List[str] = None

    def __post_init__(self):
        if self.ERROR_KEYWORDS is None:
            self.ERROR_KEYWORDS = [
                "subquery returned more than 1 value",
                "nesne başvurusu bir nesnenin örneğine ayarlanmadı",
                "sunucu hatası",
                "system.nullreferenceexception",
                "server error",
                "geçerli web isteği yürütülürken işlenmemiş özel durum",
            ]


def get_chrome_options() -> Options:
    options = Options()
    options.add_argument("--log-level=3")
    options.add_argument("--disable-logging")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_experimental_option(
        "excludeSwitches", ["enable-logging", "enable-automation"]
    )
    options.add_experimental_option("useAutomationExtension", False)
    return options


service = Service(log_path=os.devnull)
config = Config()


class DateRangeFilter:

    @staticmethod
    def parse_date(date_str: str) -> Optional[date]:
        try:
            return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
        except ValueError:
            return None

    @staticmethod
    def parse_date_range(
        start_date_str: str, end_date_str: str
    ) -> tuple[Optional[date], Optional[date]]:
        start_date = (
            DateRangeFilter.parse_date(start_date_str)
            if start_date_str.strip()
            else None
        )
        end_date = (
            DateRangeFilter.parse_date(end_date_str) if end_date_str.strip() else None
        )
        return start_date, end_date

    @staticmethod
    def is_date_in_range(
        order_date_str: str, start_date: Optional[date], end_date: Optional[date]
    ) -> bool:
        try:
            order_date = DateRangeFilter.parse_date(order_date_str)
            if order_date is None:
                return False

            if start_date and end_date:
                return start_date <= order_date <= end_date
            elif start_date:
                return order_date >= start_date
            elif end_date:
                return order_date <= end_date
            else:
                return True

        except Exception:
            return False


class FileProcessor:

    @staticmethod
    def read_txt_file(file_path: str) -> List[str]:
        with open(file_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    @staticmethod
    def read_xlsx_file(file_path: str) -> List[str]:
        import openpyxl

        wb = openpyxl.load_workbook(file_path)
        ws = wb.active
        return [str(cell.value).strip() for cell in ws["A"] if cell.value]

    @staticmethod
    def read_xls_file(file_path: str) -> List[str]:
        import xlrd

        wb = xlrd.open_workbook(file_path)
        sheet = wb.sheet_by_index(0)
        return [
            str(sheet.cell_value(i, 0)).strip()
            for i in range(sheet.nrows)
            if sheet.cell_value(i, 0)
        ]

    @staticmethod
    def read_xml_file(file_path: str) -> List[str]:
        import xml.etree.ElementTree as ET

        tree = ET.parse(file_path)
        root = tree.getroot()
        return [elem.text.strip() for elem in root.iter("kod") if elem.text]

    @classmethod
    def read_file(cls, file_path: str) -> List[str]:
        file_extension = Path(file_path).suffix.lower()

        processors = {
            ".txt": cls.read_txt_file,
            ".xlsx": cls.read_xlsx_file,
            ".xls": cls.read_xls_file,
            ".xml": cls.read_xml_file,
        }

        processor = processors.get(file_extension)
        if not processor:
            raise ValueError(f"Unsupported file format: {file_extension}")

        return processor(file_path)


class ErrorLogger:

    def __init__(self, error_file: str = config.ERROR_FILE):
        self.error_file = error_file

    def log_error(self, product_code: str):
        with open(self.error_file, "a", encoding="utf-8") as f:
            f.write(f"{product_code}\n")


class RowFilter:

    @staticmethod
    def should_process_row(
        row, tds: List, date_range: tuple = None, status_filter: str = None
    ) -> bool:
        if len(tds) < 15:
            return False

        if date_range:
            start_date, end_date = date_range
            order_date_str = tds[14].text.strip()
            if not DateRangeFilter.is_date_in_range(
                order_date_str, start_date, end_date
            ):
                return False

        if status_filter and len(tds) >= 7:
            hazirlik = tds[6].text.strip()
            if hazirlik.upper() != status_filter.upper():
                return False

        return True


class WebDriverManager:

    def __init__(self, config: Config):
        self.config = config
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None
        self.error_logger = ErrorLogger()

    @contextmanager
    def create_driver(self):
        try:
            self.driver = webdriver.Chrome(
                service=service, options=get_chrome_options()
            )
            self.wait = WebDriverWait(self.driver, self.config.WAIT_TIMEOUT)
            yield self.driver
        finally:
            if self.driver:
                self.driver.quit()

    def navigate_to_start_page(self):
        self.driver.get(self.config.START_URL)
        time.sleep(self.config.PAGE_LOAD_DELAY)

    def search_product(self, product_code: str) -> bool:
        try:
            search_input = self.wait.until(
                EC.presence_of_element_located(
                    (By.ID, "gridViewurnerede_DXFREditorcol4_I")
                )
            )
            search_input.clear()
            search_input.send_keys(product_code)

            time.sleep(self.config.SEARCH_DELAY)
            self.wait.until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, 'tr[id^="gridViewurnerede_DXDataRow"]')
                )
            )
            return True

        except TimeoutException:
            return False

    def has_error_page(self) -> bool:
        page_text = self.driver.page_source.lower()
        return any(err in page_text for err in self.config.ERROR_KEYWORDS)

    def process_product_row(
        self,
        row,
        row_index: int,
        date_range: tuple = None,
        status_filter: str = None,
    ) -> bool:
        try:
            tds = row.find_elements(By.TAG_NAME, "td")
            if len(tds) < 15:
                return False

            if not RowFilter.should_process_row(row, tds, date_range, status_filter):
                if date_range:
                    order_date = tds[14].text.strip()
                    start_date, end_date = date_range
                    if start_date and end_date:
                        self.log(
                            f"  Satır {row_index+1} tarih aralığı dışında ({order_date}), atlanıyor."
                        )
                    elif start_date:
                        self.log(
                            f"  Satır {row_index+1} başlangıç tarihinden önce ({order_date}), atlanıyor."
                        )
                    elif end_date:
                        self.log(
                            f"  Satır {row_index+1} bitiş tarihinden sonra ({order_date}), atlanıyor."
                        )
                elif status_filter:
                    hazirlik = tds[6].text.strip() if len(tds) > 6 else "N/A"
                    self.log(
                        f"  Satır {row_index+1} durum filtrelendi ({hazirlik}), atlanıyor."
                    )
                return False

            work_order = tds[2].text.strip()
            link = tds[3].find_element(By.TAG_NAME, "a")

            self.driver.execute_script("arguments[0].click();", link)
            time.sleep(self.config.CLICK_DELAY)
            self.driver.switch_to.window(self.driver.window_handles[-1])

            if self.has_error_page():
                self.error_logger.log_error(work_order)
                self.driver.close()
                self.driver.switch_to.window(self.driver.window_handles[0])
                return False

            success = self.process_product_page(work_order, row_index)

            if len(self.driver.window_handles) > 1:
                self.driver.close()
                self.driver.switch_to.window(self.driver.window_handles[0])

            return success

        except Exception as e:
            if len(self.driver.window_handles) > 1:
                self.driver.close()
                self.driver.switch_to.window(self.driver.window_handles[0])
            return False

    def process_product_page(self, work_order: str, row_index: int) -> bool:
        try:
            quantity_input = self.wait.until(
                EC.presence_of_element_located((By.ID, "Miktar_I"))
            )
            quantity = quantity_input.get_attribute("value")

            try:
                start1 = self.wait.until(EC.element_to_be_clickable((By.ID, "Baslat")))
                self.driver.execute_script("arguments[0].click();", start1)
                time.sleep(self.config.CLICK_DELAY)

                start2 = self.wait.until(
                    EC.element_to_be_clickable((By.ID, "btnUpdatebaslat"))
                )
                self.driver.execute_script("arguments[0].click();", start2)
                time.sleep(self.config.UPDATE_DELAY)
            except TimeoutException:
                pass

            finish = self.wait.until(EC.element_to_be_clickable((By.ID, "Bitir")))
            self.driver.execute_script("arguments[0].click();", finish)
            time.sleep(self.config.CLICK_DELAY)

            brut_input = self.wait.until(
                EC.presence_of_element_located((By.ID, "Brut_I"))
            )
            brut_input.clear()
            brut_input.send_keys(quantity)
            time.sleep(self.config.CLICK_DELAY)

            add_button = self.wait.until(
                EC.element_to_be_clickable((By.ID, "btnUpdate_CD"))
            )
            self.driver.execute_script("arguments[0].click();", add_button)
            time.sleep(self.config.UPDATE_DELAY)

            return True

        except TimeoutException:
            self.error_logger.log_error(work_order)
            return False
        except Exception:
            return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.driver_manager = WebDriverManager(config)
        self.product_codes: List[str] = []
        self.selected_file: Optional[str] = None
        self.is_running = False
        self.date_range = None
        self.status_filter = None

    def setup_ui(self):
        self.title("Portal Cleaner - Ultimate")
        self.geometry("900x900")

        main_frame = tk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        title_label = tk.Label(
            main_frame, text="Portal Cleaner Ultimate", font=("Arial", 16, "bold")
        )
        title_label.pack(pady=(0, 20))

        options_frame = tk.LabelFrame(
            main_frame, text="İşlem Seçenekleri", padx=10, pady=5
        )
        options_frame.pack(fill="x", pady=(0, 10))

        self.date_enabled = tk.BooleanVar(value=False)
        date_check = tk.Checkbutton(
            options_frame,
            text="Tarih Filtresi Kullan",
            variable=self.date_enabled,
            command=self.on_date_toggle,
        )
        date_check.pack(anchor="w", pady=2)

        self.date_frame = tk.LabelFrame(
            options_frame, text="Tarih Aralığı", padx=10, pady=5
        )

        start_date_frame = tk.Frame(self.date_frame)
        start_date_frame.pack(fill="x", pady=2)
        tk.Label(start_date_frame, text="Başlangıç Tarihi (dd.mm.yyyy):").pack(
            side="left"
        )
        self.start_date_entry = tk.Entry(start_date_frame, width=15)
        self.start_date_entry.pack(side="left", padx=(5, 10))
        self.start_date_entry.insert(0, "01.07.2025")

        end_date_frame = tk.Frame(self.date_frame)
        end_date_frame.pack(fill="x", pady=2)
        tk.Label(end_date_frame, text="Bitiş Tarihi (dd.mm.yyyy):").pack(side="left")
        self.end_date_entry = tk.Entry(end_date_frame, width=15)
        self.end_date_entry.pack(side="left", padx=(5, 10))
        self.end_date_entry.insert(0, "13.07.2025")

        help_frame = tk.Frame(self.date_frame)
        help_frame.pack(fill="x", pady=(5, 0))
        help_text = "Not: Başlangıç tarihi boş = sınırsız başlangıç, Bitiş tarihi boş = sınırsız bitiş"
        tk.Label(help_frame, text=help_text, fg="gray", font=("Arial", 8)).pack(
            anchor="w"
        )

        self.status_enabled = tk.BooleanVar(value=False)
        status_check = tk.Checkbutton(
            options_frame,
            text="Durum Filtresi Kullan",
            variable=self.status_enabled,
            command=self.on_status_toggle,
        )
        status_check.pack(anchor="w", pady=2)

        self.status_frame = tk.LabelFrame(
            options_frame, text="Durum Filtresi", padx=10, pady=5
        )

        status_input_frame = tk.Frame(self.status_frame)
        status_input_frame.pack(fill="x", pady=2)
        tk.Label(status_input_frame, text="Durum (örn: HAZIRLIK):").pack(side="left")
        self.status_entry = tk.Entry(status_input_frame, width=20)
        self.status_entry.pack(side="left", padx=(5, 0))
        self.status_entry.insert(0, "HAZIRLIK")

        self.product_enabled = tk.BooleanVar(value=False)
        product_check = tk.Checkbutton(
            options_frame,
            text="Ürün Kodu Dosyası Kullan",
            variable=self.product_enabled,
            command=self.on_product_toggle,
        )
        product_check.pack(anchor="w", pady=2)

        self.product_frame = tk.LabelFrame(
            options_frame, text="Ürün Kodu Dosyası", padx=10, pady=5
        )

        self.select_file_button = tk.Button(
            self.product_frame,
            text="Ürün Kodları Dosyası Seç (.txt, .xlsx, .xls, .xml)",
            command=self.select_file,
        )
        self.select_file_button.pack(pady=5)

        button_frame = tk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        self.start_button = tk.Button(
            button_frame, text="Başlat", command=self.start_processing
        )
        self.start_button.pack(pady=5)

        log_frame = tk.LabelFrame(main_frame, text="İşlem Logları", padx=10, pady=5)
        log_frame.pack(fill="both", expand=True)

        self.log_box = scrolledtext.ScrolledText(log_frame, state="disabled", height=15)
        self.log_box.pack(fill="both", expand=True)

        self.on_date_toggle()
        self.on_status_toggle()
        self.on_product_toggle()

    def on_date_toggle(self):
        if self.date_enabled.get():
            self.date_frame.pack(fill="x", pady=(5, 0))
        else:
            self.date_frame.pack_forget()

    def on_status_toggle(self):
        if self.status_enabled.get():
            self.status_frame.pack(fill="x", pady=(5, 0))
        else:
            self.status_frame.pack_forget()

    def on_product_toggle(self):
        if self.product_enabled.get():
            self.product_frame.pack(fill="x", pady=(5, 0))
        else:
            self.product_frame.pack_forget()

    def log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {text}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")
        self.update_idletasks()

    def select_file(self):
        file_path = filedialog.askopenfilename(
            title="Ürün kodları dosyasını seçin",
            filetypes=[
                ("Tüm desteklenen", "*.txt *.xlsx *.xls *.xml"),
                ("Metin Dosyası", "*.txt"),
                ("Excel Dosyası", "*.xlsx *.xls"),
                ("XML Dosyası", "*.xml"),
            ],
        )

        if file_path:
            self.selected_file = file_path
            self.log(f"Seçilen dosya: {file_path}")

    def validate_date_inputs(self) -> tuple[bool, str]:
        start_date_str = self.start_date_entry.get().strip()
        end_date_str = self.end_date_entry.get().strip()

        start_date, end_date = DateRangeFilter.parse_date_range(
            start_date_str, end_date_str
        )

        if start_date_str and start_date is None:
            return False, "Başlangıç tarihi geçersiz format (dd.mm.yyyy kullanın)"

        if end_date_str and end_date is None:
            return False, "Bitiş tarihi geçersiz format (dd.mm.yyyy kullanın)"

        if start_date and end_date and start_date > end_date:
            return False, "Başlangıç tarihi bitiş tarihinden sonra olamaz"

        return True, ""

    def start_processing(self):
        if self.is_running:
            return

        if self.date_enabled.get():
            is_valid, error_msg = self.validate_date_inputs()
            if not is_valid:
                messagebox.showerror("Hata", error_msg)
                return

        if self.status_enabled.get():
            status_value = self.status_entry.get().strip()
            if not status_value:
                messagebox.showerror("Hata", "Durum filtresi için değer giriniz!")
                return

        if self.product_enabled.get():
            if not self.selected_file:
                messagebox.showerror("Hata", "Ürün kodu dosyası seçiniz!")
                return

        self.date_range = None
        if self.date_enabled.get():
            start_date_str = self.start_date_entry.get().strip()
            end_date_str = self.end_date_entry.get().strip()
            self.date_range = DateRangeFilter.parse_date_range(
                start_date_str, end_date_str
            )

        self.status_filter = None
        if self.status_enabled.get():
            self.status_filter = self.status_entry.get().strip()

        try:
            if self.product_enabled.get():
                self.product_codes = FileProcessor.read_file(self.selected_file)
                if not self.product_codes:
                    messagebox.showerror(
                        "Hata", "Dosyada geçerli ürün kodu bulunamadı!"
                    )
                    return
                self.log(f"Toplam {len(self.product_codes)} ürün kodu yüklendi.")

            filters = []
            if self.date_enabled.get():
                start_date_str = self.start_date_entry.get().strip()
                end_date_str = self.end_date_entry.get().strip()
                filters.append(f"Tarih: {start_date_str} - {end_date_str}")

            if self.status_enabled.get():
                filters.append(f"Durum: {self.status_filter}")

            if self.product_enabled.get():
                filters.append(f"Ürün Kodu: {len(self.product_codes)} kod")

            if not filters:
                filters.append("Tüm iş emirleri")

            mode_text = " | ".join(filters)
            self.log(f"İşlem modu: {mode_text}")

            self.start_button.config(state="disabled")
            self.is_running = True

            threading.Thread(target=self.run_processing, daemon=True).start()

        except Exception as e:
            messagebox.showerror("Hata", f"Dosya okunurken hata oluştu:\n{e}")

    def run_processing(self):
        try:
            with self.driver_manager.create_driver() as driver:
                self.driver_manager.navigate_to_start_page()
                self.log("Site yüklendi, işlem başlatılıyor...")

                if self.product_enabled.get():
                    for index, product_code in enumerate(self.product_codes, start=1):
                        if not self.is_running:
                            break

                        self.log(
                            f"\n[{index}/{len(self.product_codes)}] İş emri aratılıyor: {product_code}"
                        )

                        success = False
                        for attempt in range(config.MAX_RETRIES):
                            if attempt > 0:
                                self.log(f"  Yeniden deneme #{attempt + 1}")

                            if self.driver_manager.search_product(product_code):
                                rows = driver.find_elements(
                                    By.CSS_SELECTOR,
                                    'tr[id^="gridViewurnerede_DXDataRow"]',
                                )

                                if not rows:
                                    self.log(f"  {product_code} için sonuç bulunamadı")
                                    self.error_logger.log_error(product_code)
                                    break

                                self.log(f"  {len(rows)} satır bulundu, işleniyor...")

                                for row_index, row in enumerate(rows):
                                    if self.driver_manager.process_product_row(
                                        row,
                                        row_index,
                                        self.date_range,
                                        self.status_filter,
                                    ):
                                        success = True
                                        break

                            if success:
                                break

                            time.sleep(config.SEARCH_DELAY)

                        if not success:
                            self.log(f"  {product_code} için işlem başarısız")

                        self.log("  Yeni ürüne geçiliyor.\n")
                        time.sleep(config.SEARCH_DELAY)

                else:
                    self.log("Tüm iş emirleri taranıyor...")

                    rows = driver.find_elements(
                        By.CSS_SELECTOR, 'tr[id^="gridViewurnerede_DXDataRow"]'
                    )

                    if not rows:
                        self.log("Hiç satır bulunamadı!")
                        return

                    self.log(f"{len(rows)} satır bulundu, filtreleniyor...")

                    processed_count = 0
                    for row_index, row in enumerate(rows):
                        if not self.is_running:
                            break

                        if self.driver_manager.process_product_row(
                            row,
                            row_index,
                            self.date_range,
                            self.status_filter,
                        ):
                            processed_count += 1

                    self.log(f"Toplam {processed_count} satır işlendi.")

                self.log("\nTüm işlemler tamamlandı.")

        except Exception as e:
            self.log(f"Genel hata: {e}")

        finally:
            self.is_running = False
            self.start_button.config(state="normal")


if __name__ == "__main__":
    app = App()
    app.mainloop()
