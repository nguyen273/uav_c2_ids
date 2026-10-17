#!/bin/bash
# sync_from_host.sh: Đồng bộ toàn bộ code từ Windows Shared Folder (/mnt/hgfs/nckh3) vào ~/nckh3

HOST_DIR="/mnt/hgfs/nckh3"
VM_DIR="$HOME/nckh3"

if [ ! -d "$HOST_DIR" ]; then
    echo "Lỗi: Không tìm thấy thư mục chia sẻ $HOST_DIR!"
    exit 1
fi

echo " Đang đồng bộ toàn bộ mã nguồn từ Host ($HOST_DIR/src) vào ($VM_DIR/src)..."
mkdir -p "$VM_DIR/src"
rsync -av --update --delete "$HOST_DIR/src/" "$VM_DIR/src/"

# Đồng bộ thêm các file cấu hình hoặc script ở thư mục gốc nếu có
for f in run_all.sh sync_from_host.sh requirements.txt monitor.py audit_data.py; do
    if [ -f "$HOST_DIR/$f" ]; then
        cp -u "$HOST_DIR/$f" "$VM_DIR/$f" 2>/dev/null || true
    fi
done

chmod +x "$VM_DIR"/src/pipeline/*.sh 2>/dev/null || true

echo " Hoàn tất đồng bộ! Tất cả code mới nhất đã sẵn sàng."
