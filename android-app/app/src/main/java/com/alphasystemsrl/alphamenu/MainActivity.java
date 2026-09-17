package com.alphasystemsrl.alphamenu;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.role.RoleManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    static final String EXTRA_CALLER = "alpha_menu_caller";
    private static final String BASE = "https://menu.alphasystemsrl.it";
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private WebView webView;
    private TextView state;
    private PrintBridgeServer printBridge;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        createChannels();
        if (android.os.Build.VERSION.SDK_INT >= 33 && (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED || checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED))
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS,Manifest.permission.READ_CONTACTS}, 20);
        else if (checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.READ_CONTACTS}, 21);
        printBridge = new PrintBridgeServer();
        printBridge.start();
        buildUi();
        handleIntent(getIntent());
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(Color.rgb(13,22,39));
        LinearLayout tools = new LinearLayout(this); tools.setPadding(10,8,10,8); tools.setGravity(android.view.Gravity.CENTER_VERTICAL);
        Button caller = new Button(this); caller.setText("Attiva riconoscimento chiamate"); caller.setOnClickListener(v -> requestCallRole());
        Button sync = new Button(this); sync.setText("Sincronizza clienti"); sync.setOnClickListener(v -> syncCustomers(true));
        Button test = new Button(this); test.setText("Prova banco"); test.setOnClickListener(v -> testRelay());
        state = new TextView(this); state.setTextColor(Color.WHITE); state.setPadding(10,0,0,0); state.setText("Stampa Android pronta");
        tools.addView(caller); tools.addView(sync); tools.addView(test); tools.addView(state, new LinearLayout.LayoutParams(0,LinearLayout.LayoutParams.WRAP_CONTENT,1));
        webView = new WebView(this);
        WebSettings settings = webView.getSettings(); settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW); settings.setUserAgentString(settings.getUserAgentString()+" AlphaMenuAndroid/1.0");
        CookieManager.getInstance().setAcceptCookie(true); CookieManager.getInstance().setAcceptThirdPartyCookies(webView,true);
        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient(){
            @Override public void onPageFinished(WebView view,String url){super.onPageFinished(view,url);if(url.startsWith(BASE))syncCustomers(false);}
            @Override public boolean shouldOverrideUrlLoading(WebView view,android.webkit.WebResourceRequest request){Uri uri=request.getUrl();if("menu.alphasystemsrl.it".equals(uri.getHost()))return false;startActivity(new Intent(Intent.ACTION_VIEW,uri));return true;}
        });
        root.addView(tools); root.addView(webView,new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,0,1)); setContentView(root);
    }

    private void requestCallRole(){RoleManager roles=getSystemService(RoleManager.class);if(roles.isRoleAvailable(RoleManager.ROLE_CALL_SCREENING)){if(roles.isRoleHeld(RoleManager.ROLE_CALL_SCREENING)){Toast.makeText(this,"Riconoscimento chiamate già attivo",Toast.LENGTH_SHORT).show();return;}startActivityForResult(roles.createRequestRoleIntent(RoleManager.ROLE_CALL_SCREENING),30);}else Toast.makeText(this,"Questo telefono non supporta l’identificazione chiamate",Toast.LENGTH_LONG).show();}

    private void syncCustomers(boolean announce){executor.execute(()->{try{String cookie=CookieManager.getInstance().getCookie(BASE);if(cookie==null||cookie.isEmpty())throw new IllegalStateException("Accedi prima ad Alpha Menu");HttpURLConnection connection=(HttpURLConnection)new URL(BASE+"/api/ordini/clienti?tutti=1").openConnection();connection.setRequestProperty("Cookie",cookie);connection.setRequestProperty("User-Agent","AlphaMenuAndroid/1.0");connection.setConnectTimeout(8000);connection.setReadTimeout(8000);if(connection.getResponseCode()!=200)throw new IllegalStateException("Accedi prima ad Alpha Menu");BufferedReader reader=new BufferedReader(new InputStreamReader(connection.getInputStream(),StandardCharsets.UTF_8));StringBuilder body=new StringBuilder();for(String line;(line=reader.readLine())!=null;)body.append(line);JSONObject result=new JSONObject(body.toString());CustomerStore.save(this,result.optJSONArray("clienti"));boolean relayReady=syncRelayToken(cookie);int count=result.optJSONArray("clienti")==null?0:result.optJSONArray("clienti").length();runOnUiThread(()->{state.setText(count+" clienti · "+(relayReady?"banco collegato":"banco non collegato"));if(announce)Toast.makeText(this,relayReady?"Rubrica aggiornata e banco collegato":"Rubrica aggiornata, ma banco non collegato",Toast.LENGTH_LONG).show();});}catch(Exception error){if(announce)runOnUiThread(()->Toast.makeText(this,error.getMessage(),Toast.LENGTH_LONG).show());}});}

    private boolean syncRelayToken(String cookie) {
        try {
            HttpURLConnection connection=(HttpURLConnection)new URL(BASE+"/api/ordini/chiamate/token?dispositivo="+Uri.encode(android.os.Build.MANUFACTURER+" "+android.os.Build.MODEL)).openConnection();
            connection.setRequestProperty("Cookie",cookie);
            connection.setRequestProperty("User-Agent","AlphaMenuAndroid/1.1");
            connection.setConnectTimeout(8000);connection.setReadTimeout(8000);
            if(connection.getResponseCode()!=200)return false;
            BufferedReader reader=new BufferedReader(new InputStreamReader(connection.getInputStream(),StandardCharsets.UTF_8));
            StringBuilder body=new StringBuilder();for(String line;(line=reader.readLine())!=null;)body.append(line);
            CustomerStore.saveRelayToken(this,new JSONObject(body.toString()).optString("token"));
            return !CustomerStore.relayToken(this).isEmpty();
        } catch(Exception ignored) { return false; }
    }

    private void testRelay() {
        String token=CustomerStore.relayToken(this);
        if(token.isEmpty()){Toast.makeText(this,"Premi prima Sincronizza clienti",Toast.LENGTH_LONG).show();return;}
        state.setText("Invio prova al banco…");
        executor.execute(()->{
            try {
                HttpURLConnection connection=(HttpURLConnection)new URL(BASE+"/api/ordini/chiamate/ricevuta").openConnection();
                connection.setRequestMethod("POST");connection.setDoOutput(true);
                connection.setRequestProperty("Authorization","Bearer "+token);
                connection.setRequestProperty("Content-Type","application/json; charset=utf-8");
                connection.setConnectTimeout(8000);connection.setReadTimeout(8000);
                byte[] payload=new JSONObject().put("telefono","0000000000").put("test",true).toString().getBytes(StandardCharsets.UTF_8);
                connection.setFixedLengthStreamingMode(payload.length);connection.getOutputStream().write(payload);connection.getOutputStream().close();
                int code=connection.getResponseCode();runOnUiThread(()->{state.setText(code==201?"Banco collegato":"Banco non raggiungibile");Toast.makeText(this,code==201?"Prova inviata: controlla il banco":"Collegamento non riuscito",Toast.LENGTH_LONG).show();});
            } catch(Exception error){runOnUiThread(()->{state.setText("Banco non collegato");Toast.makeText(this,"Errore collegamento: "+error.getMessage(),Toast.LENGTH_LONG).show();});}
        });
    }

    private void handleIntent(Intent intent){String phone=intent==null?null:intent.getStringExtra(EXTRA_CALLER);if(phone==null||phone.isEmpty()){if(webView.getUrl()==null)webView.loadUrl(BASE+"/ordini/evasione");return;}webView.loadUrl(BASE+"/ordini/evasione?caller="+Uri.encode(phone));}
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);handleIntent(intent);}
    @Override public void onBackPressed(){if(webView!=null&&webView.canGoBack())webView.goBack();else super.onBackPressed();}
    @Override protected void onDestroy(){if(printBridge!=null)printBridge.stop();executor.shutdownNow();if(webView!=null)webView.destroy();super.onDestroy();}
    private void createChannels(){NotificationManager manager=getSystemService(NotificationManager.class);manager.createNotificationChannel(new NotificationChannel(AlphaCallScreeningService.CHANNEL_CALLS,"Clienti in chiamata",NotificationManager.IMPORTANCE_HIGH));}
}
