import configparser
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pygetwindow as gw
import pytesseract
from PIL import Image, ImageGrab


def _preprocess_and_ocr(image: np.ndarray, lang: str, psm: int, method: str, use_gaussian_blur: bool, crop_settings: dict, opening_kernel_size: int) -> str:
    """画像データを受け取り、前処理してOCRテキストを返す"""
    if image is None:
        return ""
    gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image

    # --- 上下左右の枠線をクロップ ---
    h, w = gray_image.shape
    top, bottom, left, right = crop_settings['top'], crop_settings['bottom'], crop_settings['left'], crop_settings['right']
    gray_image = gray_image[top:h-bottom, left:w-right]

    # --- Tesseract推奨の解像度にスケーリング ---
    h, w = gray_image.shape
    target_h = 300
    max_w = 4000 # 拡大後の最大幅を設定
    if h == 0: return "" # 画像の高さが0の場合は処理を中断
    scale = target_h / h

    # アスペクト比を維持したサイズを計算
    scaled_w = int(w * scale)
    scaled_h = int(h * scale)

    # 幅が上限を超える場合は、上限に合わせて高さも再計算し、アスペクト比を維持する
    if scaled_w > max_w:
        final_h = int(scaled_h * (max_w / scaled_w))
        final_w = max_w
    else:
        final_h, final_w = scaled_h, scaled_w

    processed_image = cv2.resize(gray_image, (final_w, final_h), interpolation=cv2.INTER_CUBIC)

    if use_gaussian_blur: # 角を丸める
        # カーネルサイズは奇数で、大きいほどぼかしが強くなる
        processed_image = cv2.GaussianBlur(processed_image, (5, 5), 0)

    if method == 'adaptive':
        # adaptiveは現在使われていないが、将来のために残す
        binary_image = cv2.adaptiveThreshold(processed_image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    else:  # 'otsu' またはデフォルト
        _, binary_image = cv2.threshold(processed_image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # --- オープニング処理でノイズ除去 ---
    # 収縮(erosion)の後に膨張(dilation)を行うことで、小さなノイズを除去する
    kernel = np.ones((opening_kernel_size, opening_kernel_size), np.uint8)
    binary_image = cv2.morphologyEx(binary_image, cv2.MORPH_OPEN, kernel)

    config_str = f'--psm {psm} -c preserve_interword_spaces=1'
    return pytesseract.image_to_string(binary_image, lang=lang, config=config_str)


def capture_and_ocr():
    """
    指定されたウィンドウの特定領域をキャプチャし、OCR処理を実行する。
    """
    try:
        # --- 設定ファイルの読み込み ---
        if getattr(sys, 'frozen', False):
            base_dir = Path(sys.executable).parent
        else:
            base_dir = Path(__file__).resolve().parent
        config_file = base_dir / 'config.ini'

        if not config_file.is_file():
            print(f"エラー: 設定ファイル 'config.ini' が見つかりません。\nパス: {config_file}")
            return

        config = configparser.ConfigParser()
        config.read(str(config_file), encoding='utf-8')

        # パスと設定値を取得
        # --- Tesseract OCRのパスを解決 ---
        # 1. exeと同じ階層にあるtesseractフォルダ内のtesseract.exeを優先 (インストーラーで同梱した場合)
        # 2. 見つからない場合はconfig.iniのパス設定をフォールバックとして使用 (開発環境など)
        tesseract_path_from_config = config.get('Paths', 'Tesseract')
        bundled_tesseract_path = base_dir / 'tesseract' / 'tesseract.exe'
        
        if bundled_tesseract_path.is_file():
            tesseract_path = str(bundled_tesseract_path)
        else:
            tesseract_path = tesseract_path_from_config

        window_title = config.get('OCR', 'WindowTitle')
        output_dir = base_dir / 'output'
        ocr_output_file = output_dir / config.get('Paths', 'OcrOutputFile').split('\\')[-1]

        # 4つの領域の座標を読み込む
        regions = {
            "BankCode": config.get('Capture', 'RegionBankCode', fallback=None),
            "BankName": config.get('Capture', 'RegionBankName', fallback=None),
            "BranchCode": config.get('Capture', 'RegionBranchCode', fallback=None),
            "BranchName": config.get('Capture', 'RegionBranchName', fallback=None),
        }

        # 4つの領域の前処理設定を読み込む
        preprocess_settings = {}
        for key in regions.keys():
            section = f"Preprocess{key}"
            if config.has_section(section):
                preprocess_settings[key] = {
                    "method": config.get(section, 'BinarizationMethod', fallback='otsu').lower(),
                    "blur": config.getboolean(section, 'EnableGaussianBlur', fallback=False),
                    "crop": {
                        'top': config.getint(section, 'CropTop', fallback=0),
                        'bottom': config.getint(section, 'CropBottom', fallback=0),
                        'left': config.getint(section, 'CropLeft', fallback=0),
                        'right': config.getint(section, 'CropRight', fallback=0),
                    },
                    "opening_kernel_size": config.getint(section, 'OpeningKernelSize', fallback=2)
                }

        # Tesseractのパスを設定
        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path)

        # --- ウィンドウの検索とアクティブ化 ---
        target_windows = gw.getWindowsWithTitle(window_title)
        if not target_windows:
            print(f"エラー: タイトルに '{window_title}' を含むウィンドウが見つかりません。")
            return

        window = target_windows[0]
        if not window.isActive:
            try:
                window.activate()
                time.sleep(0.5)  # ウィンドウがアクティブになるのを待つ
            except Exception as e:
                print(f"ウィンドウのアクティブ化に失敗しました: {e}")

        # --- キャプチャの実行 ---
        output_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        captured_images = {}
        for key, region_str in regions.items():
            if region_str:
                try:
                    x, y, w, h = map(int, region_str.split(','))
                    bbox = (window.left + x, window.top + y, window.left + x + w, window.top + y + h)
                    img = ImageGrab.grab(bbox=bbox)
                    # PIL ImageをOpenCV形式(numpy配列, BGR)に変換してメモリに保持
                    img_np = np.array(img)
                    captured_images[key] = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
                    #print(f"領域 '{key}' をキャプチャしました")
                except Exception as e:
                    print(f"領域 '{key}' のキャプチャに失敗しました: {e}")

        # --- 4つの領域を個別にOCR処理 ---
        ocr_results = {}
        # 金融機関コード (英語モデル)
        if "BankCode" in captured_images:
            settings = preprocess_settings["BankCode"]
            ocr_results["BankCode"] = _preprocess_and_ocr(captured_images["BankCode"], 'eng', 6, settings["method"], settings["blur"], settings["crop"], settings["opening_kernel_size"])
        # 金融機関名 (日本語モデル)
        if "BankName" in captured_images:
            settings = preprocess_settings["BankName"]
            ocr_results["BankName"] = _preprocess_and_ocr(captured_images["BankName"], 'jpn', 6, settings["method"], settings["blur"], settings["crop"], settings["opening_kernel_size"])
        # 支店コード (英語モデル)
        if "BranchCode" in captured_images:
            settings = preprocess_settings["BranchCode"]
            ocr_results["BranchCode"] = _preprocess_and_ocr(captured_images["BranchCode"], 'eng', 6, settings["method"], settings["blur"], settings["crop"], settings["opening_kernel_size"])
        # 支店名 (日本語モデル)
        if "BranchName" in captured_images:
            settings = preprocess_settings["BranchName"]
            ocr_results["BranchName"] = _preprocess_and_ocr(captured_images["BranchName"], 'jpn', 6, settings["method"], settings["blur"], settings["crop"], settings["opening_kernel_size"])

        # --- 結果を整形して結合 ---
        bank_code = "".join(filter(str.isdigit, ocr_results.get("BankCode", "")))
        bank_name = ocr_results.get("BankName", "").strip()
        branch_code = "".join(filter(str.isdigit, ocr_results.get("BranchCode", "")))
        branch_name = ocr_results.get("BranchName", "").strip()

        ocr_text = f"{bank_code}：{bank_name}：{branch_code}：{branch_name}"

        # --- OCR結果の自動修正 ([Corrections]セクションを適用) ---
        if config.has_section('Corrections'):
            #print("config.iniの[Corrections]セクションを適用します...")
            for wrong, correct in config.items('Corrections'):
                ocr_text = ocr_text.replace(wrong, correct)

        # --- 空行を除外 ---
        lines = ocr_text.splitlines()
        non_blank_lines = [line for line in lines if line.strip()]
        final_text = "\n".join(non_blank_lines)

        # OCR結果をtxtファイルに保存
        ocr_output_file.write_text(final_text, encoding='utf-8')
        #print(f"\nOCR結果を '{ocr_output_file}' に保存しました。")
        #print("-" * 20)
        print(final_text)
        #print("-" * 20)

    except FileNotFoundError:
        print(f"エラー: Tesseractが見つかりません。config.iniのパスを確認してください: {tesseract_path}")
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # 必要なライブラリがインストールされているか確認
    try:
        import pygetwindow
        from PIL import Image, ImageGrab
    except ImportError:
        print("エラー: 必要なライブラリがインストールされていません。")
        sys.exit(1)

    capture_and_ocr()
