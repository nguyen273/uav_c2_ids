import argparse
import logging
import numpy as np
import pandas as pd
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("P2_MockData")

def generate_mock_maeng_data(output_dir, seed=42):
    """
    Sinh dữ liệu minh họa theo phương trình tự khai báo. Đây không phải dữ liệu
    Maeng, không được chứng minh cùng phân phối và không dùng cho hiệu chuẩn/
    đánh giá thực nghiệm.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    
    # Các mốc độ cao mục tiêu theo dataset của AERPAW
    altitudes = [30.0, 50.0, 70.0, 90.0, 110.0]
    
    # Khoảng cách 3D từ UAV tới trạm gốc BS (từ 50m đến 1000m)
    distances = np.linspace(50, 1000, num=500)
    
    # Thông số vô tuyến mô phỏng theo bài báo Maeng
    # Công suất phát giả định (P_tx) - Path Loss gốc (PL0)
    A_base = -30.0 
    
    # Path loss exponent ở độ cao 30m (n_h0)
    n_h0 = 2.5
    
    # Hệ số giảm n(h) khi độ cao tăng (alpha)
    alpha = 0.005 
    
    # Shadow fading (độ nhiễu tiêu chuẩn)
    sigma_fading = 3.5 

    records = []
    rng = np.random.default_rng(seed)
    
    for h in altitudes:
        # Mô hình: n(h) = n_h0 - alpha * (h - 30)
        n_h = n_h0 - alpha * (h - 30.0)
        
        for d in distances:
            # Thêm nhiễu ngẫu nhiên (Shadow fading)
            noise = rng.normal(0, sigma_fading)
            
            # Tính RSRP (hoặc received power): RSRP = A - 10 * n * log10(d) + noise
            rsrp = A_base - 10 * n_h * np.log10(d) + noise
            
            records.append({
                'distance_m': round(d, 2),
                'altitude_m_nominal': h,
                'synthetic_received_level_db': round(rsrp, 2),
                'frequency_ghz': 3.51,
                'provenance': 'synthetic_mock'
            })
            
    df = pd.DataFrame(records)
    
    csv_file = out_path / 'maeng_standardized.csv'
    df.to_csv(csv_file, index=False)
    
    logger.info(f"Đã tạo thành công {len(df)} dòng dữ liệu Mock tại: {csv_file}")
    logger.warning("Synthetic-only output; not Maeng data, calibrated dBm, or valid for empirical claims.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output_dir', type=str, default='../data/processed')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    
    generate_mock_maeng_data(args.output_dir, args.seed)
