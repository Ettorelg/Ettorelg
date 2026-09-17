package com.alphasystemsrl.alphamenu;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.telecom.Call;
import android.telecom.CallScreeningService;
import org.json.JSONObject;

public class AlphaCallScreeningService extends CallScreeningService {
    static final String CHANNEL_CALLS = "alpha_menu_calls";

    @Override public void onScreenCall(Call.Details details) {
        CallResponse response = new CallResponse.Builder()
                .setDisallowCall(false).setRejectCall(false).setSilenceCall(false)
                .setSkipCallLog(false).setSkipNotification(false).build();
        respondToCall(details, response);
        if (details.getCallDirection() != Call.Details.DIRECTION_INCOMING) return;
        Uri handle = details.getHandle();
        String phone = handle == null ? "" : handle.getSchemeSpecificPart();
        if (phone.isEmpty()) return;
        JSONObject customer = CustomerStore.find(this, phone);
        showCaller(phone, customer);
    }

    private void showCaller(String phone, JSONObject customer) {
        NotificationManager manager = getSystemService(NotificationManager.class);
        NotificationChannel channel = new NotificationChannel(CHANNEL_CALLS,
                "Clienti in chiamata", NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("Mostra il cliente Alpha Menu che sta chiamando");
        channel.enableLights(true); channel.setLightColor(Color.BLUE);
        manager.createNotificationChannel(channel);

        Intent open = new Intent(this, MainActivity.class)
                .putExtra(MainActivity.EXTRA_CALLER, phone)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pending = PendingIntent.getActivity(this, phone.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String name = customer == null ? "Cliente non ancora salvato" : customer.optString("nome", "Cliente");
        String text = (customer == null ? "Numero: " : "Apri un nuovo ordine per ") + phone;
        android.app.Notification notification = new android.app.Notification.Builder(this, CHANNEL_CALLS)
                .setSmallIcon(com.alphasystemsrl.alphamenu.R.drawable.ic_notification)
                .setContentTitle("📞 " + name).setContentText(text)
                .setStyle(new android.app.Notification.BigTextStyle().bigText(text + "\nTocca per aprire Aggiungi ordine."))
                .setContentIntent(pending).setAutoCancel(true).setCategory(android.app.Notification.CATEGORY_CALL)
                .setPriority(android.app.Notification.PRIORITY_HIGH).build();
        manager.notify(3100 + Math.abs(phone.hashCode() % 500), notification);
    }
}
