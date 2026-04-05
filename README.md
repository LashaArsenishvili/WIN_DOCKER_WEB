# 🖥️ Windows 11 Docker Web Manager

> Windows 11 ვირტუალური მანქანების მართვის სისტემა — ბრაუზერიდან, Docker-ით.

---

## 📌 რა არის ეს?

ეს პროექტი გაძლევს საშუალებას შექმნა და მართო **Windows 11 ვირტუალური მანქანები** პირდაპირ ბრაუზერიდან. არ გჭირდება არანაირი დამატებითი პროგრამა — მხოლოდ Docker და Python.

---

## ⚙️ როგორ მუშაობს

```
მომხმარებელი → ბრაუზერი (port 5000)
                    ↓
             Flask სერვერი (app_new.py)
                    ↓
             Docker Container
                    ↓
         Windows 11 VM (QEMU/KVM)
                    ↓
         noVNC ეკრანი (port 8018+)
```

---

## 🚀 ფუნქციები

- ✅ Windows 11 VM-ების შექმნა ერთი დაჭერით
- ✅ ბრაუზერიდან Windows ეკრანის ნახვა (noVNC)
- ✅ RDP კავშირის მხარდაჭერა
- ✅ მრავალი მომხმარებლის ერთდროული მართვა
- ✅ Bulk VM შექმნა (ერთდროულად მრავალი VM)
- ✅ GPU Passthrough (NVIDIA)
- ✅ KVM აქსელერაცია
- ✅ ავტომატური ISO აღმოჩენა
- ✅ რეალური დროის ლოგები
- ✅ ავტომატური აღდგენა (crash-ის შემთხვევაში)
- ✅ IP Whitelist უსაფრთხოება

---

## 📋 მოთხოვნები

| კომპონენტი | ვერსია |
|-----------|--------|
| Python | 3.8+ |
| Docker | 20.0+ |
| KVM | ჩართული BIOS-ში |
| RAM | მინ. 8GB (VM-ზე 4GB) |
| Disk | მინ. 80GB |
| OS | Linux (Kali, Ubuntu, Debian) |

---

## 🛠️ ინსტალაცია

### 1. რეპოზიტორიის ჩამოტვირთვა
```bash
git clone https://github.com/შენი-username/windows-docker-manager.git
cd windows-docker-manager
```

### 2. Python პაკეტების ინსტალაცია
```bash
pip install flask docker --break-system-packages
```

### 3. Windows ISO მომზადება

**ვარიანტი A** — ავტომატური ჩამოტვირთვა:
```bash
python setup.py
```

**ვარიანტი B** — ხელით:
- ჩამოტვირთე [Windows 11 ISO](https://www.microsoft.com/software-download/windows11)
- დააკოპირე `windows11.iso` სახელით პროექტის папке-ში

### 4. გაშვება
```bash
# Port 5000-ის გათავისუფლება (თუ დაკავებულია)
fuser -k 5000/tcp

# სერვერის გაშვება
python app_new.py
```

### 5. ბრაუზერში გახსნა
```
http://localhost:5000
```

---

## 🔐 შესვლის მონაცემები

| ველი | მნიშვნელობა |
|------|------------|
| Username | `itspecialist` |
| Password | `kali2026!` |

> ⚠️ შეცვალე პაროლი `app_new.py`-ში პროდაქშენზე გამოყენებამდე!

---

## 📁 პროექტის სტრუქტურა

```
windows-docker-manager/
├── app_new.py          # მთავარი Flask სერვერი
├── setup.py            # ISO ავტო-ჩამოტვირთვა
├── windows11.iso       # Windows ISO (თვითონ მოათავსე)
├── whitelist.txt       # IP whitelist (არასავალდებულო)
└── templates/
    ├── index.html      # მთავარი დაშბორდი
    ├── login.html      # შესვლის გვერდი
    └── blocked.html    # დაბლოკილი IP გვერდი
```

---

## 🖥️ VM მართვა

### VM შექმნა
1. გახსენი `http://localhost:5000`
2. დააჭირე **➕ New VM**
3. შეიყვანე username და password
4. დაელოდე 20-40 წუთს პირველი ინსტალაციისთვის

### VM-ის გახსნა
```
http://SERVER_IP:8010  ← პირველი VM
http://SERVER_IP:8011  ← მეორე VM
...და ა.შ.
```

### Bulk VM შექმნა
1. დააჭირე **📋 Bulk Create**
2. შეიყვანე prefix (მაგ: `user`), რაოდენობა და პაროლი
3. სისტემა ავტომატურად შექმნის `user01`, `user02`... VM-ებს

---

## ⚡ სწრაფი კლონირება (პირველი ინსტალაციის გამოტოვება)

პირველი VM-ის ინსტალაციის შემდეგ შეგიძლია სხვა VM-ები წამებში შექმნა:

```bash
# GIORGI-ს დისკის კოპირება ახალ მომხმარებელზე
cp /root/win11_sessions/GIORGI/data.img \
   /root/win11_sessions/NEWUSER/data.img
```

---

## 🔧 კონფიგურაცია

`app_new.py`-ში შეგიძლია შეცვალო:

```python
WINDOWS_VER    = "11"       # Windows ვერსია: 11, 10, server2022
WIN_RAM        = "4G"       # RAM თითო VM-ზე
WIN_CPUS       = "4"        # CPU core-ები
WIN_DISK       = "64G"      # დისკის ზომა
GPU_ENABLED    = True       # NVIDIA GPU Passthrough
VNC_PASSWORD   = "admin777" # noVNC პაროლი
PORT_WEB_START = 8010       # პირველი VM-ის პორტი
```

---

## 🐛 ხშირი პრობლემები

| პრობლემა | გამოსავალი |
|---------|-----------|
| Port 5000 დაკავებულია | `fuser -k 5000/tcp` |
| Docker არ მუშაობს | `systemctl start docker` |
| KVM არ არის | ჩართე ვირტუალიზაცია BIOS-ში |
| ISO ვერ ჩამოიტვირთება | გამოიყენე `python setup.py` |
| ეკრანი შავია | დაელოდე 3-5 წუთს, Windows იტვირთება |

---

## 📜 ლიცენზია

MIT License — გამოიყენე თავისუფლად! 🎉

---

## 🤝 წვლილი

Pull Request-ები მისაღებია! 
Issues-ები გახსენი პრობლემების შემთხვევაში.

---

*დამზადებულია ❤️-ით Kali Linux-ზე*
