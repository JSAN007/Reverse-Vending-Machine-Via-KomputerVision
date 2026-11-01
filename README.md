# EcoVision RVM

Aplikasi web prototipe **Reverse Vending Machine (RVM)** untuk memindai botol dengan kamera, mencatat setoran dalam rupiah, dan mengunduh struk PDF. Dibangun menggunakan **FastAPI, Roboflow Workflows, dan SQLite**.

**Kenali botol mu, sayangi bumi mu.**

Model klasifikasi yang digunakan pada proyek ini mengenali **AQUA, Nestle Pure Life, dan Le Minerale**. Nama merek yang ditampilkan mengikuti keluaran model Roboflow.

> Prototipe ini menggunakan konfirmasi masuk secara manual sebagai pengganti sensor. Nominal yang tercatat adalah nilai setoran; aplikasi belum melakukan pembayaran atau transfer uang.

## Fitur

- Scan kamera secara berkala dengan preview besar dan opsi layar penuh pada browser yang mendukungnya.
- Latar pastel dengan ilustrasi SVG bergerak, tombol jeda animasi, dan dukungan pengaturan reduced motion perangkat.
- Batas confidence awal **90%**, diterapkan di backend untuk kamera, upload, dan konfirmasi.
- Hasil di bawah batas tampil sebagai **Botol tidak terdeteksi** dan tidak dapat dikonfirmasi.
- Berbagai merek botol dalam satu transaksi, dengan rincian jumlah dan subtotal.
- Tarif berbeda per merek (AQUA, Nestle Pure Life, Le Minerale), dengan tarif bawaan awal **Rp100 per botol**.
- Sesi dan riwayat tersimpan di SQLite, termasuk setelah halaman dimuat ulang.
- Konfirmasi yang sama tidak menambah setoran dua kali, termasuk saat request dikirim bersamaan.
- Struk PDF **80 mm**, panjang mengikuti isi, dan dapat diunduh kembali dari riwayat.
- Ringkasan database satu baris per transaksi dan waktu tampilan **WIB**.
- CRUD transaksi selesai melalui web: tambah manual, lihat rincian/PDF, edit, dan hapus dengan konfirmasi.

## Teknologi

| Bagian | Teknologi |
| --- | --- |
| Backend dan API | Python, FastAPI, Uvicorn |
| Inferensi gambar | Roboflow Serverless Workflows melalui HTTPX |
| Antarmuka | HTML, CSS, JavaScript, kamera browser |
| Penyimpanan | SQLite bawaan Python |
| Struk | ReportLab |
| Pengujian | unittest, FastAPI TestClient, pypdf; Playwright untuk uji browser opsional |

Model diakses melalui workflow cloud sehingga proyek ini tidak memerlukan file model `.pt` lokal. Koneksi internet dan akses ke workflow Roboflow tetap diperlukan.

## Instalasi

### Prasyarat

- Python **3.11 atau lebih baru** dan pip.
- Browser dengan dukungan kamera serta izin penggunaan kamera.
- API key Roboflow dan akses ke workflow yang akan digunakan.
- Node.js hanya diperlukan untuk pemeriksaan JavaScript atau pengujian browser opsional.

### Windows / PowerShell

Unduh atau clone repository, lalu buka terminal di folder proyek.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Untuk instalasi baru, salin contoh konfigurasi:

```powershell
Copy-Item .env.example .env
```

Lewati penyalinan tersebut jika sudah memiliki `.env`. Isi `ROBOFLOW_API_KEY` di file `.env`, lalu periksa URL workflow.

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Buka **[http://127.0.0.1:8000](http://127.0.0.1:8000)**. Hentikan server dengan **Ctrl+C** di terminal.

Jika port 8000 sedang dipakai, ganti dengan `--port 8002` dan buka **[http://127.0.0.1:8002](http://127.0.0.1:8002)**. Untuk pengembangan, opsi `--reload` dapat ditambahkan pada perintah Uvicorn.

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# Instalasi baru saja; jangan menimpa .env yang sudah berisi konfigurasi.
cp .env.example .env
# Isi API key dan URL workflow di .env sebelum menjalankan server.
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

## Konfigurasi

Contoh isi `.env`:

```dotenv
ROBOFLOW_API_KEY=isi_api_key_anda
ROBOFLOW_WORKFLOW_URL=https://serverless.roboflow.com/NAMA_WORKSPACE/workflows/NAMA_WORKFLOW
BOTTLE_MIN_CONFIDENCE=0.90
# Opsional; default otomatis berada di folder data/ proyek.
# RVM_DB_PATH=data/rvm.sqlite3
```

| Variabel | Keterangan |
| --- | --- |
| `ROBOFLOW_API_KEY` | API key dengan akses ke workflow. Dibaca backend, tidak ditaruh di JavaScript. |
| `ROBOFLOW_WORKFLOW_URL` | URL lengkap endpoint workflow. `.env.example` memuat workflow proyek ini; gunakan endpoint sendiri jika memakai model berbeda. |
| `BOTTLE_MIN_CONFIDENCE` | Batas confidence dalam rentang lebih dari `0` sampai `1`. Default `0.90` berarti 90%. |
| `RVM_DB_PATH` | Lokasi database opsional. Default `data/rvm.sqlite3` di folder proyek. Path relatif mengikuti direktori kerja server. |

Restart server setelah mengubah `.env`. Di bagian **Tarif setoran**, atur harga AQUA, Nestle Pure Life, dan Le Minerale secara terpisah ketika tidak ada sesi aktif. Antarmuka hanya menampilkan tarif ketiga merek tersebut. Harga otomatis dipilih dari nama hasil scan (mengabaikan kapitalisasi, aksen, spasi, tanda hubung, dan garis bawah untuk ketiga merek). Tarif disalin ke sesi baru, sehingga perubahan berikutnya tidak mengubah sesi berjalan, riwayat, atau voucher lama.

Pengaturan lama yang hanya memiliki satu harga tetap dipertahankan untuk kompatibilitas data; tiap kolom merek awalnya mengikuti harga tersebut sampai disimpan secara terpisah. Tidak ada kolom tarif label lain di antarmuka. Tarif `0` diperbolehkan. Pada konfirmasi, backend menentukan harga dari snapshot sesi, bukan dari nominal kiriman browser.

Contoh pengaturan tarif melalui `PUT /api/rvm/settings`:

```json
{
  "price_per_bottle": 100,
  "brand_prices": {
    "aqua": 100,
    "nestle pure life": 150,
    "le minerale": 200
  }
}
```

Harga di atas adalah contoh; tentukan nilainya sesuai program setoran. Dalam satu transaksi, masing-masing botol memakai harga mereknya dan subtotalnya dijumlahkan.

## Cara menggunakan

1. Tekan **Mulai Setor**. Kamera langsung diaktifkan; izinkan akses saat browser memintanya.
2. Tunjukkan **satu botol** ke kamera. Hasil dengan confidence minimal 90% membuka konfirmasi. Jika hasil belum memenuhi batas, deteksi terus berjalan.
3. Periksa label, lalu tekan **Konfirmasi botol masuk (simulasi)** setelah botol dimasukkan. Gunakan **Scan ulang** jika hasil keliru.
4. Keluarkan botol dari area scan, lalu tekan **Botol berikutnya**. Botol berikutnya boleh berbeda merek dan tetap masuk transaksi yang sama.
5. Tekan **Selesai & Buat Struk** untuk menutup transaksi dan melihat total.
6. Pilih **Download Struk PDF**. Unduhan yang sama tersedia kembali melalui **Riwayat Setoran**.

Preview kamera tetap hidup ketika hasil menunggu konfirmasi. Pengiriman frame ke Roboflow dijeda saat hasil terkunci dan setelah botol dikonfirmasi, sampai pengguna memulai siklus berikutnya. Hasil yang belum dikonfirmasi berlaku selama **60 detik**; jika kedaluwarsa, pengguna perlu memindai ulang.

Me-refresh atau menutup halaman tidak menghapus setoran. Aktifkan kembali kamera untuk melanjutkan sesi yang tersimpan. Tekan **Perbesar** untuk membuka preview layar penuh; tekan **Perkecil** atau Esc untuk kembali. Antarmuka menggunakan kamera tanpa fitur unggah foto.

Contoh satu transaksi:

| Merek | Jumlah | Tarif | Subtotal |
| --- | ---: | ---: | ---: |
| AQUA | 2 | Rp100 | Rp200 |
| Nestle Pure Life | 1 | Rp150 | Rp150 |
| Le Minerale | 2 | Rp200 | Rp400 |
| **Total** | **5 botol** | | **Rp750** |

## Database SQLite

### Mengelola transaksi dari web

Bagian **Kelola transaksi** menyediakan **Tambah Transaksi**, **Lihat & PDF**, **Edit**, dan **Hapus**. Formulir mendukung beberapa merek, jumlah botol, serta tarif per botol; tarif merek disarankan otomatis untuk baris baru dan masih dapat diubah manual. Harga baris yang sudah diedit tidak ditimpa ketika nama merek diganti. Total rupiah dihitung ulang oleh backend. Maksimal 50 baris merek dan 1.000 botol pada transaksi manual.

Transaksi yang masih aktif harus diselesaikan melalui alur setoran sebelum diedit atau dihapus. Transaksi manual dibuat dalam status selesai. Rincian baru ditandai `entry_method=manual` dengan confidence `0`, sehingga tidak dianggap hasil validasi AI. Rincian hasil scan yang tidak berubah mempertahankan ID dan confidence aslinya. Migrasi otomatis menandai rincian lama sebagai `entry_method=camera`.

Edit menggunakan versi data untuk menolak perubahan dari formulir lama ketika transaksi sudah diubah di tab lain. Penghapusan meminta konfirmasi dan menghapus transaksi beserta rincian serta buku nilainya secara atomik. Ringkasan, view SQLite, dan PDF yang diunduh berikutnya mengikuti data terbaru. PDF yang sudah diunduh sebelumnya tetap merupakan file lama.

Tabel `transaction_changes` menyimpan aksi tambah/edit/hapus beserta salinan sebelum dan sesudah perubahan. Catatan tersebut tetap ada setelah transaksi dihapus dari daftar. Pengelolaan ini ditujukan untuk kios lokal; belum ada autentikasi admin.

Database dibuat otomatis saat server pertama kali berjalan:

```text
data/rvm.sqlite3
```

| Tabel / view | Isi |
| --- | --- |
| `sessions` | Satu baris untuk setiap sesi transaksi, status, waktu, dan snapshot tarif. |
| `deposits` | Satu baris untuk setiap botol yang dikonfirmasi, terhubung ke transaksi melalui `session_id`. |
| `value_ledger` | Catatan nominal rupiah untuk setiap setoran. |
| `settings` | Tarif yang digunakan pada sesi baru. |
| `transaction_changes` | Jejak tambah/edit/hapus, termasuk salinan data sebelum dan sesudah perubahan. |
| `transaksi` | **View satu baris per transaksi**: jumlah botol, jumlah merek, total rupiah, ringkasan merek, dan waktu WIB. |
| `rincian_transaksi` | View jumlah botol dan subtotal per merek dalam setiap transaksi. |

Untuk membaca ringkasan di **DB Browser for SQLite**, buka file database, pilih tab **Ramban Data / Browse Data**, lalu pilih **`transaksi`** pada dropdown tabel. Pilih `rincian_transaksi` untuk melihat rincian per merek. Kedua view mengikuti data terbaru, termasuk sesi aktif yang belum memiliki botol.

Contoh query:

```sql
SELECT * FROM transaksi ORDER BY mulai_wib DESC;

SELECT * FROM rincian_transaksi
WHERE transaksi_id = 'ID_TRANSAKSI';
```

Jumlah dan nominal dihitung backend menggunakan integer rupiah. Setoran dan buku nilainya ditulis dalam satu transaksi SQLite. ID hasil scan menjadi kunci unik konfirmasi, sedangkan versi sesi mencegah respons scan terlambat membuka kembali hasil yang sudah dibatalkan.

Gambar kamera tidak disimpan ke database. Timestamp asli disimpan dalam UTC; antarmuka, struk, dan view ringkasan menampilkannya dalam WIB. Untuk backup sederhana, hentikan server lalu salin seluruh folder `data/`.

## Struk PDF

PDF menggunakan judul **Voucher Belanja** dan memuat nomor voucher dari ID transaksi, tanggal terbit, nama merek, jumlah, tarif, subtotal, serta **Nilai Voucher** dalam rupiah. Formatnya menyerupai voucher thermal dengan lebar **80 mm** dan panjang mengikuti jumlah rincian; transaksi panjang dapat berlanjut ke halaman berikutnya.

PDF menggunakan teks, font bawaan, dan kompresi sehingga transaksi biasa berukuran beberapa KB. Redaksinya dibuat ringkas tanpa istilah teknis. Keterangan **Penukaran voucher belum tersedia** menjelaskan status layanan; format voucher ini belum menambahkan fitur penukaran, masa berlaku, atau kerja sama dengan gerai.

## Struktur proyek

```text
.
├── main.py                  # FastAPI, upload, pemanggilan workflow, dan endpoint
├── detection.py             # Batas confidence dan penolakan kelas negatif
├── rvm.py                   # SQLite, sesi, konfirmasi, dan view transaksi
├── receipt.py               # Pembuat struk PDF thermal
├── templates/
│   └── index.html           # Halaman aplikasi
├── static/
│   ├── css/style.css        # Tampilan responsif
│   └── js/
│       ├── main.js          # Kamera dan alur setoran
│       └── transactions.js  # Formulir CRUD transaksi
├── tests/                   # Pengujian backend dan browser
├── requirements.txt         # Dependensi aplikasi
├── requirements-dev.txt     # Dependensi tambahan untuk pengujian
├── .env.example             # Contoh konfigurasi tanpa API key
└── .gitignore
```

Folder `data/`, `.venv/`, dan `.tmp/` dibuat secara lokal dan tidak dimasukkan ke Git.

## API

Dokumentasi interaktif tersedia di **[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)** ketika server berjalan. Sesuaikan port jika menggunakan 8002.

| Metode | Endpoint | Fungsi |
| --- | --- | --- |
| `GET` | `/health` | Status server, konfigurasi workflow, dan batas confidence. Keberadaan API key bukan bukti koneksi workflow berhasil. |
| `POST` | `/predict` | Memeriksa gambar tanpa mencatat setoran; input multipart `file`. |
| `GET`, `PUT` | `/api/rvm/settings` | Membaca / mengubah tarif bawaan `price_per_bottle` dan peta tarif `brand_prices`; perubahan dikunci selama sesi aktif. |
| `POST` | `/api/rvm/sessions` | Membuka sesi atau mengembalikan sesi yang masih aktif. |
| `GET` | `/api/rvm/current` | Mengambil sesi aktif. |
| `GET` | `/api/rvm/sessions/{session_id}` | Detail sesi beserta setoran. |
| `POST` | `/api/rvm/sessions/{session_id}/scan` | Scan gambar untuk sesi; input multipart `file`. |
| `POST` | `/api/rvm/sessions/{session_id}/confirm` | Konfirmasi hasil scan melalui JSON `candidate_id`. |
| `POST` | `/api/rvm/sessions/{session_id}/next` | Membuka siklus scan berikutnya / scan ulang. |
| `POST` | `/api/rvm/sessions/{session_id}/finish` | Menutup sesi. |
| `GET` | `/api/rvm/sessions/{session_id}/receipt.pdf` | Mengunduh struk sesi yang sudah selesai. |
| `GET` | `/api/rvm/history` | Daftar sesi selesai dengan parameter `limit` dan `offset`. |
| `GET` | `/api/rvm/summary` | Ringkasan seluruh setoran. |
| `GET`, `POST` | `/api/rvm/transactions` | Daftar transaksi selesai / tambah transaksi manual melalui JSON `items`. |
| `GET`, `PUT`, `DELETE` | `/api/rvm/transactions/{session_id}` | Lihat, ubah, atau hapus transaksi. Edit memakai JSON `expected_version`; hapus memakai query `expected_version`. |

Contoh body tambah transaksi manual (`request_id` UUID opsional untuk retry yang tidak menggandakan data):

```json
{
  "items": [
    {"label": "AQUA", "quantity": 2, "price_per_bottle": 100},
    {"label": "Le Minerale", "quantity": 1, "price_per_bottle": 150}
  ]
}
```

## Pengujian

Instal dependensi pengembangan dan jalankan pengujian dari folder proyek:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Pemeriksaan sintaks JavaScript jika Node.js tersedia:

```powershell
node --check static/js/main.js
```

Pengujian backend memakai database terpisah di `.tmp/tests/` serta hasil AI tiruan, tanpa memakai kredit Roboflow. Cakupannya meliputi batas confidence, penolakan kelas negatif, transaksi campur merek, duplikasi, request bersamaan, rollback, pemulihan sesi, view transaksi, dan isi serta ukuran struk PDF.

Pengujian browser opsional tersedia pada `tests/browser_rvm.cjs` dan memerlukan Playwright beserta Chromium. Jalankan `tests/browser_server.py` dengan Python virtual environment di terminal terpisah, lalu jalankan skrip browser dengan Node.js. Server QA memakai port **8001**, database terpisah, dan AI tiruan. Endpoint `/__qa/mode/{mode}` hanya tersedia pada server QA. Gunakan `main:app` untuk aplikasi biasa.

## Batasan dan pengembangan berikutnya

- **Konfirmasi masih manual.** Kamera belum membuktikan botol benar-benar masuk, dan botol yang sama masih bisa disimulasikan lagi pada siklus berikutnya. Mesin fisik memerlukan sensor dan komunikasi perangkat yang terautentikasi.
- **Confidence bukan akurasi.** Batas 90% merupakan nilai awal, belum hasil kalibrasi pada dataset pengguna. Model klasifikasi tiga merek tetap dapat memberi skor tinggi pada benda lain. Penolakan non-botol perlu diperbaiki dengan data negatif, misalnya kelas `bukan_botol`, kemudian pelatihan ulang dan evaluasi pada gambar uji terpisah.
- **Satu kios lokal, satu sesi aktif.** Belum tersedia akun pengguna, saldo per pengguna, login admin, pembayaran, atau transfer uang. Aplikasi dikonfigurasi untuk diakses melalui `127.0.0.1`.
- **Inferensi menggunakan cloud.** Pemrosesan membutuhkan internet dan dapat memakai kredit Roboflow. Frame berikutnya dikirim setelah respons sebelumnya dan jeda 700 ms, sehingga kecepatan deteksi bergantung pada latensi workflow.
- **Model menentukan hasil.** Aplikasi belum memvalidasi material, kondisi kosong, atau keaslian botol. Kotak deteksi tampil jika workflow menyediakan koordinat objek. Mengganti URL workflow tidak melatih ulang model.

## Pemecahan masalah

| Masalah | Pemeriksaan |
| --- | --- |
| Kamera tidak tersedia | Gunakan localhost atau HTTPS, izinkan akses kamera, dan periksa aplikasi lain yang memakai kamera. |
| Semua gambar dianggap salah satu merek | Model klasifikasi membutuhkan contoh non-botol. Threshold membantu menolak skor rendah, tetapi tidak menjamin penolakan jika model terlalu yakin. |
| Botol tidak terdeteksi | Periksa pencahayaan, posisi botol, confidence keluaran model, dan nilai `BOTTLE_MIN_CONFIDENCE`. |
| API key belum disetel / autentikasi gagal | Isi `.env`, pastikan key memiliki akses ke workflow, lalu restart server. |
| Workflow timeout / tidak dapat dihubungi | Periksa internet, URL workflow, akses model, dan status layanan Roboflow. |
| Label belum terbaca | Periksa `model_prediction`, `rejection_reason`, dan `raw_result` pada respons `/predict`; sesuaikan `normalize_result` jika skema workflow berbeda. |
| Port sudah dipakai | Jalankan Uvicorn dengan port lain, misalnya `--port 8002`. |
| View transaksi belum terlihat | Jalankan server versi terbaru agar view dibuat, lalu muat ulang daftar tabel atau buka ulang database di viewer SQLite. |

## Menyiapkan repository GitHub

`.env.example` boleh masuk repository. `.env` berisi konfigurasi lokal, sedangkan `data/` berisi riwayat setoran; keduanya diabaikan oleh `.gitignore`, bersama virtual environment, database SQLite, log, dan hasil pengujian lokal.

Jika folder proyek belum menjadi repository Git, jalankan:

```bash
git init
git add .
git status --short
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/USERNAME/NAMA_REPOSITORY.git
git push -u origin main
```

Ganti `USERNAME/NAMA_REPOSITORY` dengan repository milikmu. Sebelum commit, periksa daftar file pada `git status`; API key, database lokal, dan virtual environment tidak perlu diunggah. Jika file tersebut sudah pernah dilacak Git, `.gitignore` tidak otomatis menghapusnya dari riwayat.
