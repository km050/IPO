"""뮤직 — PC 음악을 Wi-Fi로 아이폰에 보내는 Windows 프로그램 (창 화면)."""
import os
import secrets
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import server

try:
    import qrcode
except ImportError:  # pragma: no cover
    qrcode = None

BG, CARD, TEXT, MUTED, ACCENT = '#111114', '#1c1c21', '#f3f3f5', '#9a9aa5', '#ff5a5f'
FONT = 'Malgun Gothic' if sys.platform == 'win32' else 'Helvetica'


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = server.load_config()
        self.srv = server.MusicServer(self.cfg)

        root.title('뮤직 — 아이폰 연결')
        root.configure(bg=BG)
        root.resizable(False, False)
        try:
            root.iconbitmap(server.resource_path('icons/app.ico'))
        except Exception:
            pass

        pad = {'padx': 24}
        tk.Label(root, text='뮤직', font=(FONT, 22, 'bold'), bg=BG, fg=TEXT).pack(anchor='w', pady=(20, 0), **pad)
        tk.Label(root, text='아이폰과 같은 Wi-Fi에서 카메라로 아래 QR을 찍으세요.',
                 font=(FONT, 10), bg=BG, fg=MUTED).pack(anchor='w', **pad)

        self.qr = tk.Canvas(root, width=260, height=260, bg='white', highlightthickness=0)
        self.qr.pack(pady=16)

        self.url_var = tk.StringVar()
        tk.Label(root, textvariable=self.url_var, font=(FONT, 12, 'bold'), bg=BG, fg=TEXT).pack()
        pinrow = tk.Frame(root, bg=BG)
        pinrow.pack(pady=(4, 0))
        tk.Label(pinrow, text='PIN', font=(FONT, 11), bg=BG, fg=MUTED).pack(side='left')
        self.pin_var = tk.StringVar()
        tk.Label(pinrow, textvariable=self.pin_var, font=(FONT, 20, 'bold'), bg=BG, fg=ACCENT).pack(side='left', padx=8)
        self._btn(pinrow, 'PIN 바꾸기', self.new_pin).pack(side='left', padx=4)

        box = tk.Frame(root, bg=CARD)
        box.pack(fill='x', padx=24, pady=16)
        tk.Label(box, text='음악 폴더', font=(FONT, 10), bg=CARD, fg=MUTED).pack(anchor='w', padx=14, pady=(12, 0))
        self.folder_var = tk.StringVar()
        tk.Label(box, textvariable=self.folder_var, font=(FONT, 10), bg=CARD, fg=TEXT,
                 wraplength=330, justify='left').pack(anchor='w', padx=14)
        row = tk.Frame(box, bg=CARD)
        row.pack(fill='x', padx=14, pady=(8, 12))
        self.count_var = tk.StringVar()
        tk.Label(row, textvariable=self.count_var, font=(FONT, 10), bg=CARD, fg=MUTED).pack(side='left')
        self._btn(row, '폴더 열기', self.open_folder).pack(side='right')
        self._btn(row, '폴더 바꾸기', self.pick_folder).pack(side='right', padx=6)
        self._btn(row, '다시 읽기', self.rescan).pack(side='right')

        self.status_var = tk.StringVar()
        tk.Label(root, textvariable=self.status_var, font=(FONT, 9), bg=BG, fg=MUTED).pack(pady=(0, 18))

        self.start()
        self.tick()

    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, font=(FONT, 9), bg='#2a2a31', fg=TEXT,
                         activebackground='#3a3a44', activeforeground=TEXT, relief='flat', padx=10, pady=3,
                         cursor='hand2', bd=0)

    # ---------- server ----------
    def start(self):
        try:
            self.srv.start()
        except OSError as e:
            messagebox.showerror('뮤직', f"포트 {self.cfg['port']}을(를) 열 수 없어요.\n"
                                      f"이미 프로그램이 켜져 있는지 확인하세요.\n\n{e}")
            self.root.destroy()
            sys.exit(1)
        self.refresh()

    def refresh(self):
        self.url_var.set(self.srv.url(False))
        self.pin_var.set(self.cfg['pin'])
        self.folder_var.set(self.cfg['folder'])
        n = len(self.srv.library.order)
        if not os.path.isdir(self.cfg['folder']):
            self.count_var.set('⚠ 폴더가 없어요')
        else:
            self.count_var.set(f'{n}곡')
        self.draw_qr(self.srv.url(True))

    def draw_qr(self, text):
        c = self.qr
        c.delete('all')
        if not qrcode:
            c.create_text(130, 130, text='QR 모듈 없음\n주소를 직접 입력하세요', justify='center')
            return
        q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(text)
        q.make(fit=True)
        m = q.get_matrix()
        cell = 260 / len(m)
        for y, row in enumerate(m):
            for x, on in enumerate(row):
                if on:
                    c.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell, fill='black', outline='black')

    def tick(self):
        now = time.time()
        active = [ip for ip, t in self.srv.clients.items() if now - t < 120]
        self.status_var.set(f'● 실행 중 · 연결된 기기 {len(active)}대  —  이 창을 닫으면 연결이 끊겨요'
                            if active else '● 실행 중 · 아이폰 연결을 기다리는 중  —  이 창을 닫으면 연결이 끊겨요')
        ip_url = self.srv.url(False)
        if ip_url != self.url_var.get():  # Wi-Fi가 바뀐 경우
            self.refresh()
        self.root.after(3000, self.tick)

    # ---------- actions ----------
    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.cfg['folder'], title='음악 폴더 선택')
        if d:
            self.cfg['folder'] = os.path.normpath(d)
            server.save_config(self.cfg)
            self.srv.library.folder = self.cfg['folder']
            self.rescan()

    def rescan(self):
        self.srv.library.folder = self.cfg['folder']
        self.srv.library.scan()
        self.refresh()

    def open_folder(self):
        if os.path.isdir(self.cfg['folder']) and hasattr(os, 'startfile'):
            os.startfile(self.cfg['folder'])

    def new_pin(self):
        if not messagebox.askyesno('뮤직', 'PIN을 바꾸면 연결된 아이폰은 다시 연결해야 해요.\n바꿀까요?'):
            return
        self.cfg['pin'] = f'{secrets.randbelow(10000):04d}'
        server.save_config(self.cfg)
        self.srv.clients.clear()
        self.refresh()


def main():
    root = tk.Tk()
    App(root)
    root.protocol('WM_DELETE_WINDOW', lambda: (root.destroy(), os._exit(0)))
    root.mainloop()


if __name__ == '__main__':
    main()
