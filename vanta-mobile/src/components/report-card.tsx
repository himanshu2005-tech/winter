import { ScrollView, StyleSheet, Text, View } from 'react-native';

export function ReportCard({ content }: { content: string }) {
  return (
    <View style={styles.card}>
      <ScrollView horizontal><Text style={styles.text}>{content}</Text></ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { backgroundColor: '#111318', borderRadius: 16, padding: 16 },
  text: { color: '#f5f7fb', fontSize: 15, lineHeight: 23 },
});
